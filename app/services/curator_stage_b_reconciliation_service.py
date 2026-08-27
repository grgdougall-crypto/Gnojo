from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.services.curator_targeted_verification_service import (
    CuratorTargetedVerificationService,
)
from app.services.curator_workflow_lifecycle_service import (
    CuratorWorkflowLifecycleService,
)
from curator.checks import CuratorChecks
from curator.memory import (
    CuratorMemoryConflictError,
    CuratorMemoryError,
    CuratorMemoryLockError,
    CuratorMemorySnapshot,
    CuratorMemoryStore,
)
from curator.models import InventoryRecord
from curator.reconciliation import (
    StageBJournalError,
    StageBJournalEvent,
    StageBJournalRepository,
)


class StageBReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class StageBTaskPlan:
    task_id: str
    finding_id: str
    eligible: bool
    reason: str
    verification_status: str
    affected_fingerprint: str
    idempotency_key: str
    precondition_fingerprint: str
    before_task_fingerprint: str
    after_task_fingerprint: str
    declared_mutation_fields: tuple[str, ...]
    proposed_delta: dict[str, Any]
    state_after: dict[str, Any] | None


@dataclass(frozen=True)
class StageBTaskResult:
    task_id: str
    status: str
    reason: str
    idempotency_key: str
    proposed_delta: dict[str, Any]


@dataclass(frozen=True)
class StageBRunResult:
    run_id: str
    correlation_id: str
    capability_id: str
    capability_version: int
    dry_run: bool
    task_results: tuple[StageBTaskResult, ...]


class CuratorStageBReconciliationService:
    """Supervise one allowlisted Curator-state-only reconciliation capability."""

    CAPABILITY_ID = "cur-wr-progress-verification-refresh"
    CAPABILITY_VERSION = 1
    RULE = "CUR-WR-PROGRESS"
    FINDING_TYPE = "workflow_reasoning_progress_inconsistency"
    ACTIONABLE = frozenset({"open", "in_progress"})
    MUTATION_FIELDS = (
        "current_verification", "last_verified_fingerprint", "history"
    )

    def __init__(
        self,
        repository_root: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        lock_timeout: float = 2.0,
    ):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.journal = StageBJournalRepository(self.root / "curation_memory")
        self.lifecycle = CuratorWorkflowLifecycleService(self.root)
        self.checks = CuratorChecks(self.root)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock_timeout = lock_timeout

    def run(
        self,
        *,
        task_id: str | None = None,
        trigger_source: str = "manual",
        correlation_id: str = "",
        dry_run: bool = False,
    ) -> StageBRunResult:
        if trigger_source not in {"manual", "scheduled"}:
            raise StageBReconciliationError("Stage B trigger source is invalid.")
        if correlation_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", correlation_id):
            raise StageBReconciliationError("Stage B correlation ID is invalid.")
        run_id = self._run_id()
        initial = self.memory.snapshot()
        control_error = self._control_error(initial.state, trigger_source)
        if control_error:
            return StageBRunResult(
                run_id, correlation_id, self.CAPABILITY_ID,
                self.CAPABILITY_VERSION, dry_run,
                (StageBTaskResult(task_id or "", "FAILED", control_error, "", {}),),
            )
        task_ids = self._task_ids(initial, task_id)
        if task_id and not task_ids:
            task_ids = (task_id,)
        results = tuple(
            self._process_task(
                selected, run_id=run_id, correlation_id=correlation_id,
                trigger_source=trigger_source, dry_run=dry_run,
            )
            for selected in task_ids
        )
        return StageBRunResult(
            run_id, correlation_id, self.CAPABILITY_ID,
            self.CAPABILITY_VERSION, dry_run, results,
        )

    def _process_task(
        self,
        task_id: str,
        *,
        run_id: str,
        correlation_id: str,
        trigger_source: str,
        dry_run: bool,
    ) -> StageBTaskResult:
        for attempt in range(2):
            snapshot = self.memory.snapshot()
            plan = self._plan(snapshot, task_id)
            if dry_run:
                return self._result(plan, "DRY_RUN" if plan.eligible else "SKIPPED")
            self._before_commit(plan, attempt)
            try:
                return self._commit(
                    plan, run_id=run_id, correlation_id=correlation_id,
                    trigger_source=trigger_source,
                )
            except CuratorMemoryConflictError:
                if attempt == 0:
                    continue
                return self._record_failure(
                    plan, "Curator memory changed during both bounded attempts.",
                    run_id=run_id, correlation_id=correlation_id,
                )
            except (CuratorMemoryLockError, StageBJournalError, CuratorMemoryError) as error:
                return self._record_failure(
                    plan, str(error), run_id=run_id, correlation_id=correlation_id,
                )
            except Exception as error:
                return self._record_failure(
                    plan, f"Stage B reconciliation failed ({type(error).__name__}).",
                    run_id=run_id, correlation_id=correlation_id,
                )
        return self._failed(plan, "Stage B reconciliation exhausted its bounded retry.")

    def _commit(
        self,
        plan: StageBTaskPlan,
        *,
        run_id: str,
        correlation_id: str,
        trigger_source: str,
    ) -> StageBTaskResult:
        with self.memory.locked(timeout=self.lock_timeout) as memory:
            current = memory.snapshot()
            if current.fingerprint != plan.precondition_fingerprint:
                raise CuratorMemoryConflictError(
                    "Curator memory changed after the Stage B plan was created."
                )
            self.journal.validate_all()
            history = self.journal.get(plan.idempotency_key)
            control_error = self._control_error(current.state, trigger_source)
            if control_error:
                return self._append_terminal(
                    plan, "FAILED", control_error,
                    run_id=run_id, correlation_id=correlation_id,
                    previous=history[-1] if history else None,
                )
            task = current.state.get("tasks", {}).get(plan.task_id) or {}
            committed_in_task = (
                (task.get("current_verification") or {}).get(
                    "stage_b_idempotency_key"
                ) == plan.idempotency_key
            )
            if self.journal.committed(plan.idempotency_key):
                return self._append_terminal(
                    plan, "SKIPPED", "The exact reconciliation is already committed.",
                    run_id=run_id, correlation_id=correlation_id,
                    previous=history[-1] if history else None,
                )
            if committed_in_task:
                return self._append_terminal(
                    plan, "COMMITTED",
                    "Recovered the journal after an interrupted post-commit write.",
                    run_id=run_id, correlation_id=correlation_id,
                    previous=history[-1] if history else None,
                    after_task_fingerprint=self._fingerprint(task),
                )
            if not plan.eligible:
                return self._append_terminal(
                    plan, "SKIPPED", plan.reason,
                    run_id=run_id, correlation_id=correlation_id,
                    previous=history[-1] if history else None,
                )
            prepared = self._event(
                plan, "PREPARED", "Exact operational-state mutation prepared.",
                run_id=run_id, correlation_id=correlation_id,
                previous=history[-1] if history else None,
            )
            self.journal.append(prepared)
            persisted = memory.compare_and_swap(
                plan.precondition_fingerprint, plan.state_after or {},
                touch_updated_at=False,
            )
            after = persisted.state.get("tasks", {}).get(plan.task_id) or {}
            if self._fingerprint(after) != plan.after_task_fingerprint:
                raise CuratorMemoryError(
                    "Committed Stage B task state does not match the approved plan."
                )
            committed = self._event(
                plan, "COMMITTED", "Verification state refreshed without lifecycle changes.",
                run_id=run_id, correlation_id=correlation_id,
                previous=prepared,
                after_task_fingerprint=self._fingerprint(after),
            )
            self.journal.append(committed)
            return self._result(plan, "COMMITTED", committed.reason)

    def _plan(self, snapshot: CuratorMemorySnapshot, task_id: str) -> StageBTaskPlan:
        state = snapshot.state
        task = state.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            return self._skip_plan(snapshot, task_id, {}, "Knowledge Task was not found.")
        finding_id = self._safe_identity(
            task.get("finding_id") or task.get("durable_identity") or task_id,
            prefix="FND",
        )
        if (
            task.get("curator_rule") != self.RULE
            or task.get("finding_type") != self.FINDING_TYPE
        ):
            return self._skip_plan(snapshot, task_id, task, "Task is not a supported progress finding.")
        if str(task.get("status") or "").casefold() not in self.ACTIONABLE:
            return self._skip_plan(snapshot, task_id, task, "Task is not actionable.")
        workflow_id, separator, node_id = str(
            task.get("content_identifier") or ""
        ).partition(":")
        if not workflow_id or (separator and node_id):
            return self._skip_plan(snapshot, task_id, task, "Workflow identity is ambiguous.")
        drafts = self.lifecycle.drafts(workflow_id)
        if len(drafts) > 1:
            return self._skip_plan(snapshot, task_id, task, "Multiple editable workflows match the task.")
        target = self.lifecycle.resolve(workflow_id)
        if not target or target.workflow_id != workflow_id:
            return self._skip_plan(snapshot, task_id, task, "Authoritative workflow is unavailable.")
        workflow = target.workflow
        affected_fingerprint = CuratorTargetedVerificationService.fingerprint(workflow)
        record = InventoryRecord(
            "workflow", workflow_id, str(workflow.get("name") or workflow_id),
            target.source_path, str(workflow.get("category") or ""),
            str(workflow.get("platform") or ""), target.lifecycle, workflow,
        )
        exact = [
            finding for finding in self.checks.run_record(record)
            if finding.rule == self.RULE
            and finding.content_identifier == task.get("content_identifier")
            and finding.finding_type == self.FINDING_TYPE
        ]
        status = "still_detected" if exact else "appears_corrected"
        idempotency_key = self._idempotency_key(
            task, status=status, affected_fingerprint=affected_fingerprint
        )
        verified_at = self.now().isoformat()
        verification = {
            "verified_at": verified_at,
            "rule": self.RULE,
            "workflow_id": workflow_id,
            "node_id": "",
            "status": status,
            "message": (
                "The current workflow still matches the deterministic progress condition."
                if exact else
                "The deterministic progress condition is absent from the current workflow."
            ),
            "human_approval_required": True,
            "affected_fingerprint": affected_fingerprint,
            "stage_b_capability_id": self.CAPABILITY_ID,
            "stage_b_capability_version": self.CAPABILITY_VERSION,
            "stage_b_idempotency_key": idempotency_key,
        }
        event = {
            "at": verified_at,
            "actor": "Curator Stage B",
            "event": "targeted_verification",
            "verification_result": status,
            "rule": self.RULE,
            "workflow_id": workflow_id,
            "node_id": "",
            "stage_b_idempotency_key": idempotency_key,
        }
        after_task = deepcopy(task)
        after_task["current_verification"] = verification
        after_task["last_verified_fingerprint"] = affected_fingerprint
        history = after_task.setdefault("history", [])
        if not any(
            item.get("stage_b_idempotency_key") == idempotency_key for item in history
        ):
            history.append(event)
        after_state = deepcopy(state)
        after_state["tasks"][task_id] = after_task
        before_task_fingerprint = self._fingerprint(task)
        after_task_fingerprint = self._fingerprint(after_task)
        changed = tuple(
            field for field in self.MUTATION_FIELDS
            if task.get(field) != after_task.get(field)
        )
        if set(changed) - set(self.MUTATION_FIELDS):
            raise StageBReconciliationError("Stage B plan exceeds its mutation allowlist.")
        return StageBTaskPlan(
            task_id, finding_id, True, "Deterministic progress verification is available.",
            status, affected_fingerprint, idempotency_key, snapshot.fingerprint,
            before_task_fingerprint, after_task_fingerprint, changed,
            {
                "current_verification": {
                    "before": deepcopy(task.get("current_verification")),
                    "after": deepcopy(verification),
                },
                "last_verified_fingerprint": {
                    "before": task.get("last_verified_fingerprint"),
                    "after": affected_fingerprint,
                },
                "history_event": deepcopy(event) if "history" in changed else None,
            },
            after_state,
        )

    def _skip_plan(
        self, snapshot: CuratorMemorySnapshot, task_id: str,
        task: dict[str, Any], reason: str,
    ) -> StageBTaskPlan:
        finding_id = self._safe_identity(
            task.get("finding_id") or task.get("durable_identity") or task_id or "UNKNOWN",
            prefix="FND",
        )
        task_identity = self._safe_identity(task_id or "UNKNOWN", prefix="TASK")
        before = self._fingerprint(task)
        key = hashlib.sha256(
            "|".join((self.CAPABILITY_ID, str(self.CAPABILITY_VERSION), task_identity,
                      finding_id, "SKIPPED", before)).encode("utf-8")
        ).hexdigest()
        return StageBTaskPlan(
            task_identity, finding_id, False, reason, "SKIPPED", "", key,
            snapshot.fingerprint, before, before, (), {}, None,
        )

    def _append_terminal(
        self, plan: StageBTaskPlan, status: str, reason: str, *,
        run_id: str, correlation_id: str,
        previous: StageBJournalEvent | None = None,
        after_task_fingerprint: str | None = None,
    ) -> StageBTaskResult:
        event = self._event(
            plan, status, reason, run_id=run_id, correlation_id=correlation_id,
            previous=previous, after_task_fingerprint=after_task_fingerprint,
        )
        self.journal.append(event)
        return self._result(plan, status, reason)

    def _event(
        self, plan: StageBTaskPlan, status: str, reason: str, *,
        run_id: str, correlation_id: str,
        previous: StageBJournalEvent | None,
        after_task_fingerprint: str | None = None,
    ) -> StageBJournalEvent:
        prepared_identity = (
            previous if status == "COMMITTED" and previous
            and previous.status == "PREPARED" else None
        )
        identity = prepared_identity or plan
        return StageBJournalEvent.build(
            previous=previous,
            event_id=f"SBE-{uuid4().hex[:16].upper()}",
            run_id=run_id,
            correlation_id=correlation_id,
            capability_id=(
                prepared_identity.capability_id
                if prepared_identity else self.CAPABILITY_ID
            ),
            capability_version=(
                prepared_identity.capability_version
                if prepared_identity else self.CAPABILITY_VERSION
            ),
            task_id=identity.task_id,
            finding_id=identity.finding_id,
            idempotency_key=identity.idempotency_key,
            precondition_fingerprint=identity.precondition_fingerprint,
            before_task_fingerprint=identity.before_task_fingerprint,
            after_task_fingerprint=(
                after_task_fingerprint
                if after_task_fingerprint is not None
                else plan.after_task_fingerprint
            ),
            declared_mutation_fields=plan.declared_mutation_fields,
            at=self.now().isoformat(),
            status=status,
            reason=reason,
        )

    def _failed(self, plan: StageBTaskPlan, reason: str) -> StageBTaskResult:
        return self._result(plan, "FAILED", reason)

    def _record_failure(
        self, plan: StageBTaskPlan, reason: str, *, run_id: str, correlation_id: str
    ) -> StageBTaskResult:
        try:
            with self.memory.locked(timeout=self.lock_timeout) as memory:
                del memory
                self.journal.validate_all()
                history = self.journal.get(plan.idempotency_key)
                self._append_terminal(
                    plan, "FAILED", reason, run_id=run_id,
                    correlation_id=correlation_id,
                    previous=history[-1] if history else None,
                )
        except (CuratorMemoryError, StageBJournalError):
            pass
        return self._failed(plan, reason)

    @staticmethod
    def _result(
        plan: StageBTaskPlan, status: str, reason: str | None = None
    ) -> StageBTaskResult:
        return StageBTaskResult(
            plan.task_id, status, reason or plan.reason,
            plan.idempotency_key, deepcopy(plan.proposed_delta),
        )

    @classmethod
    def _idempotency_key(
        cls, task: dict[str, Any], *, status: str, affected_fingerprint: str
    ) -> str:
        payload = "|".join((
            cls.CAPABILITY_ID,
            str(cls.CAPABILITY_VERSION),
            str(task.get("task_id") or ""),
            str(task.get("finding_id") or task.get("durable_identity") or ""),
            status,
            affected_fingerprint,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        payload = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _control_error(cls, state: dict[str, Any], trigger_source: str) -> str:
        controls = state.get("controls") or {}
        if controls.get("global_disabled"):
            return "Curator is globally disabled by a human operator."
        if trigger_source == "scheduled" and controls.get("scheduled_runs_disabled", True):
            return "Scheduled Curator runs are disabled by a human operator."
        return ""

    def _task_ids(
        self, snapshot: CuratorMemorySnapshot, task_id: str | None
    ) -> tuple[str, ...]:
        tasks = snapshot.state.get("tasks", {})
        if task_id:
            return (task_id,) if task_id in tasks else ()
        return tuple(sorted(
            identifier for identifier, task in tasks.items()
            if task.get("curator_rule") == self.RULE
            and task.get("finding_type") == self.FINDING_TYPE
            and str(task.get("status") or "").casefold() in self.ACTIONABLE
        ))

    def _run_id(self) -> str:
        return f"STB-{self.now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12].upper()}"

    @staticmethod
    def _safe_identity(value: Any, *, prefix: str) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", text):
            return text
        return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24].upper()}"

    def _before_commit(self, plan: StageBTaskPlan, attempt: int) -> None:
        """Narrow test seam; production reconciliation performs no action here."""


class CuratorTerminalEvidenceStageBReconciliationService(
    CuratorStageBReconciliationService
):
    """Refresh one terminal-evidence task without invoking mutating verification."""

    CAPABILITY_ID = "cur-wr-terminal-evidence-verification-refresh"
    CAPABILITY_VERSION = 1
    RULE = "CUR-WR-TERMINAL-EVIDENCE"
    FINDING_TYPE = "workflow_reasoning_evidence_gap"

    def _plan(self, snapshot: CuratorMemorySnapshot, task_id: str) -> StageBTaskPlan:
        state = snapshot.state
        task = state.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            return self._skip_plan(snapshot, task_id, {}, "Knowledge Task was not found.")
        finding_id = self._safe_identity(
            task.get("finding_id") or task.get("durable_identity") or task_id,
            prefix="FND",
        )
        if (
            task.get("curator_rule") != self.RULE
            or task.get("finding_type") != self.FINDING_TYPE
        ):
            return self._skip_plan(
                snapshot, task_id, task,
                "Task is not a supported terminal-evidence finding.",
            )
        if str(task.get("status") or "").casefold() not in self.ACTIONABLE:
            return self._skip_plan(snapshot, task_id, task, "Task is not actionable.")
        if task.get("content_type") != "workflow_node":
            return self._skip_plan(
                snapshot, task_id, task,
                "Terminal-evidence content type is not workflow_node.",
            )
        identifier = str(task.get("content_identifier") or "")
        if identifier.count(":") != 1:
            return self._skip_plan(
                snapshot, task_id, task,
                "Workflow and terminal identity are ambiguous.",
            )
        workflow_id, node_id = identifier.split(":", 1)
        if not workflow_id or not node_id:
            return self._skip_plan(
                snapshot, task_id, task,
                "Workflow and terminal identity are ambiguous.",
            )
        related = task.get("related_workflows") or []
        if related and set(related) != {workflow_id}:
            return self._skip_plan(
                snapshot, task_id, task,
                "Task workflow identity is inconsistent.",
            )
        structural_terminal = str(
            (task.get("structured_evidence") or {}).get("terminal") or ""
        )
        if structural_terminal and structural_terminal != node_id:
            return self._skip_plan(
                snapshot, task_id, task,
                "Task terminal identity is inconsistent.",
            )
        drafts = self.lifecycle.drafts(workflow_id)
        if len(drafts) > 1:
            return self._skip_plan(
                snapshot, task_id, task,
                "Multiple editable workflows match the task.",
            )
        target = self.lifecycle.resolve(workflow_id)
        if not target or target.workflow_id != workflow_id:
            return self._skip_plan(
                snapshot, task_id, task,
                "Authoritative workflow is unavailable.",
            )
        workflow = target.workflow
        nodes = workflow.get("nodes")
        if not isinstance(nodes, dict):
            return self._skip_plan(
                snapshot, task_id, task,
                "Authoritative workflow cannot be analyzed deterministically.",
            )
        terminal = nodes.get(node_id)
        if not isinstance(terminal, dict):
            return self._skip_plan(
                snapshot, task_id, task, "Affected terminal node is unavailable."
            )
        if terminal.get("type") != "resolution":
            return self._skip_plan(
                snapshot, task_id, task,
                "Affected node is no longer a terminal resolution.",
            )
        affected_fingerprint = CuratorTargetedVerificationService.fingerprint(workflow)
        record = InventoryRecord(
            "workflow", workflow_id, str(workflow.get("name") or workflow_id),
            target.source_path, str(workflow.get("category") or ""),
            str(workflow.get("platform") or ""), target.lifecycle, workflow,
        )
        exact = [
            finding for finding in self.checks.run_record(record)
            if finding.rule == self.RULE
            and finding.content_identifier == identifier
            and finding.finding_type == self.FINDING_TYPE
        ]
        if len(exact) > 1:
            return self._skip_plan(
                snapshot, task_id, task,
                "Multiple matching terminal-evidence findings are ambiguous.",
            )
        if exact and exact[0].identifier != task.get("finding_id"):
            return self._skip_plan(
                snapshot, task_id, task,
                "Current finding identity does not match the task.",
            )
        status = "still_detected" if exact else "appears_corrected"
        idempotency_key = self._terminal_idempotency_key(
            task,
            workflow_id=workflow_id,
            node_id=node_id,
            status=status,
            affected_fingerprint=affected_fingerprint,
        )
        verified_at = self.now().isoformat()
        verification = {
            "verified_at": verified_at,
            "rule": self.RULE,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "status": status,
            "message": (
                "The current workflow still matches the deterministic terminal-evidence condition."
                if exact else
                "The deterministic terminal-evidence condition is absent from the current workflow."
            ),
            "human_approval_required": True,
            "affected_fingerprint": affected_fingerprint,
            "affected_fingerprint_scope": "whole_workflow",
            "stage_b_capability_id": self.CAPABILITY_ID,
            "stage_b_capability_version": self.CAPABILITY_VERSION,
            "stage_b_idempotency_key": idempotency_key,
        }
        event = {
            "at": verified_at,
            "actor": "Curator Stage B",
            "event": "targeted_verification",
            "verification_result": status,
            "rule": self.RULE,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "stage_b_idempotency_key": idempotency_key,
        }
        after_task = deepcopy(task)
        after_task["current_verification"] = verification
        after_task["last_verified_fingerprint"] = affected_fingerprint
        history = after_task.setdefault("history", [])
        if not any(
            item.get("stage_b_idempotency_key") == idempotency_key
            for item in history
        ):
            history.append(event)
        after_state = deepcopy(state)
        after_state["tasks"][task_id] = after_task
        before_task_fingerprint = self._fingerprint(task)
        after_task_fingerprint = self._fingerprint(after_task)
        all_changed = {
            field for field in set(task) | set(after_task)
            if task.get(field) != after_task.get(field)
        }
        if not all_changed.issubset(self.MUTATION_FIELDS):
            raise StageBReconciliationError(
                "Terminal-evidence plan exceeds its mutation allowlist."
            )
        changed = tuple(
            field for field in self.MUTATION_FIELDS
            if task.get(field) != after_task.get(field)
        )
        return StageBTaskPlan(
            task_id, finding_id, True,
            "Deterministic terminal-evidence verification is available.",
            status, affected_fingerprint, idempotency_key, snapshot.fingerprint,
            before_task_fingerprint, after_task_fingerprint, changed,
            {
                "identity": {
                    "task_id": task_id,
                    "finding_id": finding_id,
                    "workflow_id": workflow_id,
                    "terminal_node_id": node_id,
                },
                "verification_result": status,
                "affected_fingerprint": affected_fingerprint,
                "affected_fingerprint_scope": "whole_workflow",
                "current_verification": {
                    "before": deepcopy(task.get("current_verification")),
                    "after": deepcopy(verification),
                },
                "last_verified_fingerprint": {
                    "before": task.get("last_verified_fingerprint"),
                    "after": affected_fingerprint,
                },
                "history_event": deepcopy(event) if "history" in changed else None,
                "unchanged": {
                    "task_lifecycle": True,
                    "trusted_content": True,
                    "publication": True,
                },
            },
            after_state,
        )

    @classmethod
    def _terminal_idempotency_key(
        cls,
        task: dict[str, Any],
        *,
        workflow_id: str,
        node_id: str,
        status: str,
        affected_fingerprint: str,
    ) -> str:
        payload = "|".join((
            cls.CAPABILITY_ID,
            str(cls.CAPABILITY_VERSION),
            str(task.get("task_id") or ""),
            str(task.get("finding_id") or task.get("durable_identity") or ""),
            workflow_id,
            node_id,
            status,
            affected_fingerprint,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
