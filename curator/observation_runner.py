from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.services.content_quality_service import ContentQualityService
from app.services.curator_progress_auto_repair_policy_service import (
    CuratorProgressAutoRepairPolicyService,
)
from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_lifecycle_projection_service import (
    WorkflowLifecycleProjectionService,
)
from curator.auditor import CuratorAuditor
from curator.memory import CuratorMemoryError, CuratorMemoryStore

from .observation_models import (
    FAILED,
    PURE_OBSERVATION,
    RUNNING,
    SKIPPED_OVERLAP,
    SUCCEEDED,
    ObservationJobDefinition,
    ObservationPayload,
    ObservationRunResult,
)
from .observation_repository import (
    ObservationLock,
    ObservationOverlapError,
    ObservationResultRepository,
)


class ObservationRunnerError(RuntimeError):
    pass


class ObservationDisabledError(ObservationRunnerError):
    pass


class CuratorObservationRunner:
    """Execute allowlisted Stage A observations without changing application state."""

    JOBS = {
        name: ObservationJobDefinition(name)
        for name in ("health", "audit", "integrity", "progress-policy", "analytics")
    }
    LIFECYCLE_VERSION = "workflow-lifecycle-projection-v1"

    def __init__(
        self,
        repository_root: Path,
        *,
        results_root: Path | None = None,
        memory_root: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.root = Path(repository_root).resolve()
        self.results = ObservationResultRepository(
            results_root or self.root / "curation_observations"
        )
        self.memory_root = Path(memory_root or self.root / "curation_memory").resolve()
        self.lock_path = self.root / ".curator-observation.lock"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.handlers: dict[str, Callable[[], ObservationPayload]] = {
            "health": self._health,
            "audit": self._audit,
            "integrity": self._integrity,
            "progress-policy": self._progress_policy,
            "analytics": self._analytics,
        }

    def run(
        self,
        job_type: str,
        *,
        trigger_source: str = "manual",
        scheduler_correlation_id: str = "",
        execution_class: str = PURE_OBSERVATION,
    ) -> ObservationRunResult:
        job = self.JOBS.get(str(job_type or ""))
        if not job:
            raise ObservationRunnerError("Observation job is not allowlisted.")
        if execution_class != PURE_OBSERVATION or job.execution_class != PURE_OBSERVATION:
            raise ObservationRunnerError("Stage A supports PURE_OBSERVATION only.")
        if trigger_source not in {"manual", "scheduled"}:
            raise ObservationRunnerError("Observation trigger source is invalid.")
        correlation_id = self._correlation_id(scheduler_correlation_id)
        started = self.now()
        run_id = self._run_id(started)
        running = ObservationRunResult(
            run_id=run_id,
            job_type=job.job_type,
            execution_class=job.execution_class,
            trigger_source=trigger_source,
            scheduler_correlation_id=correlation_id,
            repository_identity=self._repository_identity(),
            application_identity="gnojo-local",
            started_at=started.isoformat(),
            completed_at="",
            duration_seconds=None,
            status=RUNNING,
            observation_counts=(),
            summary=(),
            warnings=(),
            errors=(),
            policy_versions=(),
            lifecycle_versions=(),
            trusted_content_changed=False,
            curator_state_changed=False,
            operational_result_written=True,
        )
        self.results.create(running)

        disabled_reason = self._disabled_reason(trigger_source)
        if disabled_reason:
            return self._finish(running, FAILED, errors=(disabled_reason,))

        try:
            with ObservationLock(
                self.lock_path,
                job_type=job.job_type,
                run_id=run_id,
                acquired_at=started.isoformat(),
            ):
                payload = self.handlers[job.job_type]()
            return self._finish(running, SUCCEEDED, payload=payload)
        except ObservationOverlapError:
            return self._finish(
                running,
                SKIPPED_OVERLAP,
                warnings=("Another repository observation already owns the lock.",),
            )
        except Exception as error:
            return self._finish(
                running,
                FAILED,
                errors=(f"Observation failed ({type(error).__name__}).",),
            )

    def _finish(
        self,
        running: ObservationRunResult,
        status: str,
        *,
        payload: ObservationPayload | None = None,
        warnings: tuple[str, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> ObservationRunResult:
        completed = self.now()
        payload = payload or ObservationPayload()
        result = replace(
            running,
            completed_at=completed.isoformat(),
            duration_seconds=max(
                0.0, (completed - datetime.fromisoformat(running.started_at)).total_seconds()
            ),
            status=status,
            observation_counts=payload.observation_counts,
            summary=payload.summary,
            warnings=payload.warnings + warnings,
            errors=errors,
            policy_versions=payload.policy_versions,
            lifecycle_versions=payload.lifecycle_versions,
        )
        self.results.update(result)
        return result

    def _disabled_reason(self, trigger_source: str) -> str:
        try:
            controls = CuratorMemoryStore(self.memory_root).load().get("controls", {})
        except CuratorMemoryError:
            return "Curator controls are unavailable; observation was denied."
        if controls.get("global_disabled"):
            return "Curator is globally disabled by a human operator."
        if trigger_source == "scheduled" and controls.get("scheduled_runs_disabled", True):
            return "Scheduled Curator runs are disabled by a human operator."
        return ""

    def _health(self) -> ObservationPayload:
        required = {
            "workflow_drafts": self.root / "app" / "workflow_drafts",
            "workflow_publications": self.root / "app" / "workflow_publications",
            "knowledge_base": self.root / "knowledge_base",
        }
        missing = tuple(name for name, path in required.items() if not path.is_dir())
        memory_readable = True
        try:
            state = CuratorMemoryStore(self.memory_root).load()
        except CuratorMemoryError:
            state = {"tasks": {}}
            memory_readable = False
        return ObservationPayload(
            observation_counts=(
                ("required_directories", len(required)),
                ("missing_directories", len(missing)),
                ("curator_tasks", len(state.get("tasks", {}))),
            ),
            summary=(
                ("repository_available", self.root.is_dir()),
                ("curator_memory_readable", memory_readable),
            ),
            warnings=tuple(f"Required repository area is unavailable: {item}." for item in missing),
        )

    def _audit(self) -> ObservationPayload:
        result, location = CuratorAuditor(
            self.root,
            self.root / "curation_runs",
            self.memory_root,
        ).audit(write=False)
        if location is not None:
            raise ObservationRunnerError("Pure audit unexpectedly produced a report location.")
        summary = result.summary()
        return ObservationPayload(
            observation_counts=(
                ("inventory_records", len(result.inventory)),
                ("findings", len(result.findings)),
                ("coverage_gaps", len(result.coverage)),
            ),
            summary=(
                ("defects", int(summary["findings_by_classification"].get("defect", 0))),
                ("risks", int(summary["findings_by_classification"].get("risk", 0))),
            ),
        )

    def _integrity(self) -> ObservationPayload:
        report = KnowledgeIntegrityService(self.root).report()
        workflow_ids = self._draft_workflow_ids()
        lifecycle_counts: dict[str, int] = {}
        projection_service = WorkflowLifecycleProjectionService(self.root)
        for workflow_id in workflow_ids:
            projected = projection_service.project(workflow_id)
            lifecycle_counts[projected.lifecycle_state] = (
                lifecycle_counts.get(projected.lifecycle_state, 0) + 1
            )
        counts = report.get("counts") or {}
        return ObservationPayload(
            observation_counts=tuple(sorted(
                [(str(key), int(value)) for key, value in counts.items()
                 if isinstance(value, int)]
                + [("workflow_lifecycle_projections", len(workflow_ids))]
            )),
            summary=tuple(sorted(
                (f"lifecycle_{key.lower()}", value)
                for key, value in lifecycle_counts.items()
            )),
            lifecycle_versions=(self.LIFECYCLE_VERSION,),
        )

    def _progress_policy(self) -> ObservationPayload:
        try:
            tasks = CuratorMemoryStore(self.memory_root).load().get("tasks", {})
        except CuratorMemoryError as error:
            raise ObservationRunnerError("Curator task inventory is unavailable.") from error
        supported = sorted(
            task_id for task_id, task in tasks.items()
            if task.get("curator_rule") == "CUR-WR-PROGRESS"
            and task.get("finding_type") == "workflow_reasoning_progress_inconsistency"
        )
        service = CuratorProgressAutoRepairPolicyService(self.root)
        eligible = 0
        versions = set()
        for task_id in supported:
            result = service.evaluate(task_id)
            eligible += result.eligible
            versions.add(f"{result.policy_id}:v{result.policy_version}")
        return ObservationPayload(
            observation_counts=(
                ("supported_tasks", len(supported)),
                ("eligible", eligible),
                ("ineligible", len(supported) - eligible),
            ),
            summary=(("automatic_execution_enabled", False),),
            policy_versions=tuple(sorted(versions)),
            lifecycle_versions=(self.LIFECYCLE_VERSION,),
        )

    def _analytics(self) -> ObservationPayload:
        workflows, versions = self._runtime_workflows()
        history_path = self.root / "app" / "troubleshooting_history"
        records = (
            TroubleshootingHistoryService(history_path).list(
                500, environment="production"
            )
            if history_path.is_dir() else []
        )
        report = ContentQualityService().build(
            workflows, records, {}, workflow_versions=versions
        )
        confusing = sum(
            item.get("kind") == "confusing_step"
            for item in report.get("action_queue", ())
        )
        return ObservationPayload(
            observation_counts=(
                ("production_sessions", len(records)),
                ("workflows", len(workflows)),
                ("quality_findings", len(report.get("action_queue", ()))),
                ("frequently_confusing_steps", confusing),
            ),
            summary=(("session_environment", "production"),),
        )

    def _draft_workflow_ids(self) -> tuple[str, ...]:
        directory = self.root / "app" / "workflow_drafts"
        values = set()
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
            workflow = self._read_json(path)
            workflow_id = str(workflow.get("workflow_id") or "") if workflow else ""
            if workflow_id:
                values.add(workflow_id)
        return tuple(sorted(values))

    def _runtime_workflows(self) -> tuple[dict[str, dict], dict[str, int | None]]:
        workflows: dict[str, dict] = {}
        versions: dict[str, int | None] = {}
        builtins = self.root / "app" / "decision_trees"
        for path in sorted(builtins.glob("*.json")) if builtins.is_dir() else ():
            workflow = self._read_json(path)
            workflow_id = str(workflow.get("workflow_id") or path.stem) if workflow else ""
            if workflow_id and workflow:
                workflows[workflow_id] = workflow
                versions[workflow_id] = None
        publications = self.root / "app" / "workflow_publications"
        for directory in sorted(publications.iterdir()) if publications.is_dir() else ():
            if not directory.is_dir():
                continue
            manifest = self._read_json(directory / "current.json")
            version = manifest.get("current_version") if manifest else None
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                continue
            snapshot = self._read_json(directory / f"v{version:04d}.json")
            workflow = snapshot.get("workflow") if snapshot else None
            if isinstance(workflow, dict) and workflow.get("workflow_id") == directory.name:
                workflows[directory.name] = workflow
                versions[directory.name] = version
        return workflows, versions

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _repository_identity(self) -> str:
        return hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _correlation_id(value: str) -> str:
        value = str(value or "").strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise ObservationRunnerError("Scheduler correlation ID is invalid.")
        return value

    @staticmethod
    def _run_id(started: datetime) -> str:
        return f"OBS-{started.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12].upper()}"
