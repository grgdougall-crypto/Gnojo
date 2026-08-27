from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.services.curator_stage_b_reconciliation_service import (
    CuratorEarlyConvergenceStageBReconciliationService,
    CuratorSignalRetentionStageBReconciliationService,
    CuratorStageBReconciliationService,
    StageBReconciliationError,
    StageBTaskPlan,
)
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.reconciliation import StageBJournalError, StageBJournalRepository
from curator.stage_b_scheduled_repository import StageBScheduledRunRepository


class StageBScheduledRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduledCapability:
    capability_id: str
    capability_version: int
    rule: str
    finding_type: str
    content_type: str
    service_type: type[CuratorStageBReconciliationService]

    def declaration(self) -> dict[str, Any]:
        return {"id": self.capability_id, "version": self.capability_version}


@dataclass(frozen=True)
class StageBScheduledRunResult:
    runner_id: str
    correlation_id: str
    dry_run: bool
    status: str
    summary: dict[str, Any]


class CuratorStageBScheduledRunner:
    """Run only the two accepted Stage B capabilities under scheduled authority."""

    ALLOWLIST = (
        ScheduledCapability(
            "cur-wr-early-convergence-verification-refresh", 1,
            "CUR-WR-EARLY-CONVERGENCE",
            "workflow_reasoning_early_convergence", "workflow_node",
            CuratorEarlyConvergenceStageBReconciliationService,
        ),
        ScheduledCapability(
            "cur-wr-signal-retention-verification-refresh", 1,
            "CUR-WR-SIGNAL-RETENTION", "workflow_reasoning_signal_loss",
            "workflow_node", CuratorSignalRetentionStageBReconciliationService,
        ),
    )

    def __init__(
        self,
        repository_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.root = Path(repository_root).resolve()
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.journal = StageBJournalRepository(self.root / "curation_memory")
        self.results = StageBScheduledRunRepository(self.root / "curation_memory")
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        dry_run: bool = False,
        correlation_id: str = "",
        max_candidates: int = 5,
    ) -> StageBScheduledRunResult:
        if not 1 <= int(max_candidates) <= 5:
            raise StageBScheduledRunnerError(
                "Stage B scheduled max candidates must be between 1 and 5."
            )
        if correlation_id and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", correlation_id
        ):
            raise StageBScheduledRunnerError(
                "Stage B scheduled correlation ID is invalid."
            )
        runner_id = self._runner_id()
        correlation = correlation_id or runner_id
        snapshot = self.memory.snapshot()
        control_error = self._control_error(snapshot.state)
        if control_error:
            raise StageBScheduledRunnerError(control_error)
        try:
            self.journal.validate_all()
        except StageBJournalError as error:
            raise StageBScheduledRunnerError(str(error)) from error

        queues = self._discover(snapshot.state)
        summary = self._empty_summary(queues)
        record = self._record(runner_id, correlation, "RUNNING", summary)
        if not dry_run:
            self.results.create_running(record)

        status = "SUCCEEDED_NO_CHANGES"
        failure = ""
        executable = 0
        current_capability: ScheduledCapability | None = None
        try:
            for capability, task_id in self._round_robin(queues):
                current_capability = capability
                summary["last_processed_task"] = task_id
                self.journal.validate_all()
                service = capability.service_type(self.root, now=self.now)
                plan = service.plan_task(task_id)
                counts = summary["per_capability_counts"][capability.capability_id]
                if not plan.eligible:
                    summary["preflight_skipped_count"] += 1
                    counts["preflight_skipped"] += 1
                    continue
                if self.journal.committed(plan.idempotency_key):
                    summary["committed_no_op_count"] += 1
                    counts["committed_no_op"] += 1
                    continue
                if executable >= max_candidates:
                    break
                executable += 1
                counts["would_execute"] += 1
                if dry_run:
                    continue
                result = service.run(
                    task_id=task_id,
                    trigger_source="scheduled",
                    correlation_id=correlation,
                )
                task_result = result.task_results[0] if result.task_results else None
                if task_result is None:
                    failure = "Scheduled capability returned no task result."
                    summary["failed_count"] += 1
                    counts["failed"] += 1
                    break
                if task_result.status == "COMMITTED":
                    summary["committed_count"] += 1
                    counts["committed"] += 1
                    continue
                if task_result.status == "SKIPPED":
                    summary["runtime_skipped_count"] += 1
                    counts["runtime_skipped"] += 1
                    continue
                failure = task_result.reason or "Scheduled capability failed."
                summary["failed_count"] += 1
                counts["failed"] += 1
                break
        except (
            CuratorMemoryError,
            StageBJournalError,
            StageBReconciliationError,
        ) as error:
            failure = str(error)
            summary["failed_count"] += 1
            if current_capability is not None:
                summary["per_capability_counts"][
                    current_capability.capability_id
                ]["failed"] += 1
        except Exception as error:
            failure = f"Stage B scheduled runner failed ({type(error).__name__})."
            summary["failed_count"] += 1
            if current_capability is not None:
                summary["per_capability_counts"][
                    current_capability.capability_id
                ]["failed"] += 1

        summary["failure_reason"] = failure
        if failure:
            status = (
                "PARTIAL_FAILED" if summary["committed_count"] else "FAILED"
            )
        elif summary["committed_count"]:
            status = "SUCCEEDED"
        elif dry_run and executable:
            status = "SUCCEEDED"

        if not dry_run:
            final = self._record(runner_id, correlation, status, summary)
            self.results.finalize(runner_id, final)
        return StageBScheduledRunResult(
            runner_id, correlation, dry_run, status, deepcopy(summary)
        )

    def _discover(
        self, state: dict[str, Any],
    ) -> dict[ScheduledCapability, tuple[str, ...]]:
        tasks = state.get("tasks") or {}
        return {
            capability: tuple(sorted(
                task_id for task_id, task in tasks.items()
                if isinstance(task, dict)
                and str(task.get("status") or "").casefold() in {"open", "in_progress"}
                and task.get("curator_rule") == capability.rule
                and task.get("finding_type") == capability.finding_type
                and task.get("content_type") == capability.content_type
            ))
            for capability in self.ALLOWLIST
        }

    def _round_robin(
        self, queues: dict[ScheduledCapability, tuple[str, ...]],
    ):
        positions = {capability: 0 for capability in self.ALLOWLIST}
        while True:
            yielded = False
            for capability in self.ALLOWLIST:
                position = positions[capability]
                queue = queues[capability]
                if position < len(queue):
                    yield capability, queue[position]
                    positions[capability] = position + 1
                    yielded = True
            if not yielded:
                return

    @staticmethod
    def _control_error(state: dict[str, Any]) -> str:
        controls = state.get("controls") or {}
        if controls.get("global_disabled"):
            return "Curator is globally disabled by a human operator."
        if controls.get("scheduled_runs_disabled", True):
            return "Scheduled Curator runs are disabled by a human operator."
        if controls.get("stage_b_scheduled_runs_disabled", True):
            return "Scheduled Stage B runs are disabled by a human operator."
        return ""

    def _empty_summary(
        self, queues: dict[ScheduledCapability, tuple[str, ...]],
    ) -> dict[str, Any]:
        per_capability = {
            capability.capability_id: {
                "version": capability.capability_version,
                "discovered": len(queues[capability]),
                "preflight_skipped": 0,
                "committed_no_op": 0,
                "would_execute": 0,
                "committed": 0,
                "runtime_skipped": 0,
                "failed": 0,
            }
            for capability in self.ALLOWLIST
        }
        return {
            "allowlisted_capabilities": [
                capability.declaration() for capability in self.ALLOWLIST
            ],
            "discovered_count": sum(len(queue) for queue in queues.values()),
            "preflight_skipped_count": 0,
            "committed_no_op_count": 0,
            "committed_count": 0,
            "runtime_skipped_count": 0,
            "failed_count": 0,
            "per_capability_counts": per_capability,
            "last_processed_task": "",
            "failure_reason": "",
        }

    def _record(
        self,
        runner_id: str,
        correlation_id: str,
        status: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "runner_id": runner_id,
            "correlation_id": correlation_id,
            "trigger_source": "scheduled",
            "started_at": getattr(self, "_started_at", "") or self._start_time(),
            "completed_at": "" if status == "RUNNING" else self.now().isoformat(),
            "status": status,
            **deepcopy(summary),
        }

    def _start_time(self) -> str:
        self._started_at = self.now().isoformat()
        return self._started_at

    def _runner_id(self) -> str:
        return f"STBS-{self.now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:10].upper()}"
