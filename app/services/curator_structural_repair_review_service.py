from __future__ import annotations

from pathlib import Path
from typing import Any

from app.repositories.structural_repair_approval_repository import (
    StructuralRepairApprovalRepository,
    StructuralRepairApprovalRepositoryError,
)
from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
    StructuralRepairApplicationRepositoryError,
)
from app.services.curator_fix_session_service import (
    CuratorFixSessionError,
    CuratorFixSessionService,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_apply_service import (
    CuratorStructuralRepairApplyService,
    StructuralRepairApplyError,
)
from app.services.curator_structural_repair_contracts import StructuralRepairPlan
from app.services.curator_structural_repair_preview_service import (
    CuratorStructuralRepairPreviewService,
)
from app.services.curator_task_service import CuratorTaskService
from app.services.workflow_draft_persistence import (
    WorkflowDraftPersistence,
    WorkflowDraftPersistenceError,
)
from curator.memory import CuratorMemoryError


class StructuralRepairReviewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CuratorStructuralRepairReviewService:
    """Read-only presentation boundary for one supervised structural repair."""

    ACTIONABLE = frozenset({"open", "in_progress"})

    def __init__(self, repository_root: Path):
        self.root = Path(repository_root).resolve()
        self.tasks = CuratorTaskService(self.root)
        self.sessions = CuratorFixSessionService(self.root)
        self.registry = CuratorRepairAdapterRegistry()
        self.persistence = WorkflowDraftPersistence(self.root / "app" / "workflow_drafts")
        self.approvals = StructuralRepairApprovalRepository(self.root / "curation_memory")
        self.applications = StructuralRepairApplicationRepository(self.root / "curation_memory")

    def applied_state(self, task_id: str) -> dict[str, Any] | None:
        """Project a completed repair only while its exact draft remains current."""
        try:
            application_ids = self.applications.list_application_ids()
            for application_id in reversed(application_ids):
                history = self.applications.get(application_id)
                if not history:
                    continue
                record = history[-1]
                if record.task_id != task_id or record.outcome != "applied":
                    continue
                with self.persistence.locked(Path(record.workflow_path).name) as draft:
                    current = draft.read()
                if (current.raw_sha256 != record.expected_workflow_raw_sha256_after
                        or current.semantic_sha256
                        != record.expected_workflow_semantic_sha256_after):
                    continue
                stored = self.approvals.get(record.approval_id)
                approval = stored["approval"]
                if (approval.task_id != task_id
                        or approval.preview_digest != record.preview_digest):
                    continue
                return {
                    "applied": True,
                    "application_id": record.application_id,
                    "workflow_filename": Path(record.workflow_path).name,
                    "applied_at": record.applied_at,
                    "route_changes": self.route_changes(stored["preview"]),
                }
        except (StructuralRepairApplicationRepositoryError,
                StructuralRepairApprovalRepositoryError,
                WorkflowDraftPersistenceError, OSError, ValueError):
            return None
        return None

    @staticmethod
    def route_changes(preview: dict[str, Any]) -> tuple[dict[str, str], ...]:
        """Shape the approved before/after edges for presentation without inference."""
        changes = []
        proposed = preview.get("proposed") if isinstance(preview, dict) else None
        raw_changes = proposed.get("changed_predecessor_edges") if isinstance(proposed, dict) else None
        if not isinstance(raw_changes, list):
            return ()
        for item in raw_changes:
            before = item.get("before") if isinstance(item, dict) else None
            after = item.get("after") if isinstance(item, dict) else None
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            values = {
                "source": str(before.get("source") or ""),
                "route": str(before.get("route") or ""),
                "before_destination": str(before.get("destination") or ""),
                "after_destination": str(after.get("destination") or ""),
            }
            if all(values.values()):
                changes.append(values)
        return tuple(changes)

    def preview(self, task_id: str, fix_session_id: str) -> dict[str, Any]:
        task, fix_session, item = self._task_context(task_id, fix_session_id)
        workflow_id, separator, _ = str(task.get("content_identifier") or "").partition(":")
        if not separator or not workflow_id:
            raise StructuralRepairReviewError("preview_unavailable", "The affected workflow is unavailable.")
        filename = f"{workflow_id}.json"
        try:
            with self.persistence.locked(filename) as draft:
                snapshot = draft.read()
        except Exception as error:
            raise StructuralRepairReviewError(
                "preview_unavailable", "The editable workflow draft is unavailable."
            ) from error
        if snapshot.workflow.get("workflow_id") != workflow_id:
            raise StructuralRepairReviewError(
                "preview_unavailable", "The editable workflow identity does not match this task."
            )
        preview = self.registry.preview(task, snapshot.workflow)
        if not preview.get("available"):
            raise StructuralRepairReviewError(
                "preview_unavailable", "A governed structural repair preview is not currently available."
            )
        try:
            plan = StructuralRepairPlan.from_dict(preview["plan"])
            apply_service = CuratorStructuralRepairApplyService(self.root)
            candidate = CuratorStructuralRepairPreviewService().simulate(snapshot.workflow, preview)
            apply_service._assert_exact_graph(snapshot.workflow, candidate, preview, plan)
            baseline = apply_service._reasoning_findings(snapshot.workflow)
            validation = apply_service._validate_candidate(candidate, task, baseline)
        except (KeyError, ValueError, StructuralRepairApplyError) as error:
            raise StructuralRepairReviewError(
                "preview_unavailable", "The governed repair preview could not be validated."
            ) from error
        return {
            "task": task,
            "fix_session": fix_session,
            "item": item,
            "workflow_id": workflow_id,
            "workflow_name": snapshot.workflow.get("name") or workflow_id.replace("_", " ").title(),
            "workflow_filename": filename,
            "preview": preview,
            "validation": {
                "passed": bool(validation.get("passed")),
                "schema_valid": bool(validation.get("schema", {}).get("is_valid")),
                "routes_resolved": not bool(validation.get("schema", {}).get("broken_transitions")),
                "no_cycles": not any(
                    item.get("code") == "CYCLE_DETECTED"
                    for item in validation.get("schema", {}).get("quality", {}).get("findings", [])
                ),
                "all_paths_terminate": not bool(
                    validation.get("schema", {}).get("quality", {}).get("nonterminating_paths")
                ),
                "reasoning_defect_corrected": bool(validation.get("reasoning_finding_absent")),
                "no_new_reasoning_findings": not bool(validation.get("new_reasoning_findings")),
            },
        }

    def approved(self, approval_id: str, fix_session_id: str) -> dict[str, Any]:
        try:
            stored = self.approvals.get(approval_id)
        except StructuralRepairApprovalRepositoryError as error:
            raise StructuralRepairReviewError("approval_missing", "The repair approval was not found.") from error
        approval = stored["approval"]
        task, fix_session, item = self._task_context(approval.task_id, fix_session_id)
        if (approval.fix_session_id != fix_session_id
                or approval.reviewer_identity != fix_session.get("started_by")):
            raise StructuralRepairReviewError(
                "approval_invalid", "The repair approval belongs to a different reviewer or Fix Wizard session."
            )
        return {
            "approval": approval,
            "approval_state": stored["state"],
            "preview": stored["preview"],
            "task": task,
            "fix_session": fix_session,
            "item": item,
        }

    def _task_context(self, task_id: str, fix_session_id: str):
        try:
            fix_session = self.sessions.get(fix_session_id)
            task = self.tasks.get(task_id)
        except (CuratorFixSessionError, CuratorMemoryError) as error:
            raise StructuralRepairReviewError(
                "context_invalid", "The task or Fix Wizard session is no longer available."
            ) from error
        if fix_session.get("ended_at"):
            raise StructuralRepairReviewError("context_invalid", "The Fix Wizard session has ended.")
        matches = [
            item for item in fix_session.get("repair_queue", [])
            if item.get("status", "open") == "open"
            and str(item.get("affected_content", {}).get("task_id") or "") == task_id
        ]
        if len(matches) != 1 or task.get("status", "open") not in self.ACTIONABLE:
            raise StructuralRepairReviewError(
                "context_invalid", "This task is not actionable in the selected Fix Wizard session."
            )
        eligibility = task.get("repair_eligibility") or self.registry.eligibility(task)
        if not eligibility.get("supervised_apply_available"):
            raise StructuralRepairReviewError(
                "preview_unavailable", "This task is not eligible for supervised structural repair."
            )
        return task, fix_session, matches[0]
