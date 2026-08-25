from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
    StructuralRepairRecoveryRepositoryError,
)
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_task_service import CuratorTaskService
from app.services.workflow_draft_persistence import (
    WorkflowDraftPersistence,
    WorkflowDraftPersistenceError,
)


class StructuralRepairRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CuratorStructuralRepairRecoveryService:
    """Same-authority exact-byte recovery for one successfully applied repair."""

    ACTIONABLE = frozenset({"open", "in_progress"})

    def __init__(self, repository_root: Path):
        self.root = Path(repository_root).resolve()
        curator_root = self.root / "curation_memory"
        self.applications = StructuralRepairApplicationRepository(curator_root)
        self.recoveries = StructuralRepairRecoveryRepository(curator_root)
        self.persistence = WorkflowDraftPersistence(self.root / "app" / "workflow_drafts")
        self.tasks = CuratorTaskService(self.root)
        self.sessions = CuratorFixSessionService(self.root)
        self.now = lambda: datetime.now(timezone.utc)
        self.lock_timeout = 2.0

    def context(self, application_id: str, fix_session_id: str) -> dict[str, Any]:
        history = self.applications.get(application_id)
        if not history or history[-1].outcome != "applied":
            raise StructuralRepairRecoveryError(
                "recovery_unavailable", "Only a successfully applied repair can be restored."
            )
        application = history[-1]
        material = self.recoveries.get(application_id)
        try:
            fix_session = self.sessions.get(fix_session_id)
            task = self.tasks.get(application.task_id)
        except Exception as error:
            raise StructuralRepairRecoveryError(
                "context_invalid", "The originating task or Fix Wizard session is unavailable."
            ) from error
        matches = [
            item for item in fix_session.get("repair_queue", [])
            if str(item.get("affected_content", {}).get("task_id") or "") == application.task_id
        ]
        if (fix_session.get("ended_at") or len(matches) != 1
                or task.get("status") not in self.ACTIONABLE
                or task.get("finding_id", "") != application.finding_id
                or fix_session.get("started_by") != application.reviewer_identity
                or fix_session_id != application.fix_session_id):
            raise StructuralRepairRecoveryError(
                "context_invalid", "Recovery authority does not match the applied repair."
            )
        self._match_material(application, material)
        return {
            "application": application,
            "material": material,
            "task": task,
            "fix_session": fix_session,
            "item": matches[0],
        }

    def restore(self, application_id: str, *, reviewer_identity: str,
                fix_session_id: str, reason: str) -> dict[str, Any]:
        context = self.context(application_id, fix_session_id)
        application = context["application"]
        material = context["material"]
        if str(reviewer_identity or "").strip() != application.reviewer_identity:
            raise StructuralRepairRecoveryError(
                "context_invalid", "Recovery reviewer identity does not match the application."
            )
        bounded_reason = str(reason or "").strip()
        if not bounded_reason or len(bounded_reason) > 1000:
            raise StructuralRepairRecoveryError(
                "recovery_invalid", "Explain why the editable draft is being restored."
            )
        existing_events = self.recoveries.events(application_id)
        if any(item.get("outcome") == "recovered" for item in existing_events):
            raise StructuralRepairRecoveryError(
                "recovery_unavailable", "This structural repair was already restored."
            )
        filename = Path(application.workflow_path).name
        try:
            with self.persistence.locked(filename, timeout=self.lock_timeout) as draft:
                current = draft.read()
                if (current.raw_sha256 != application.expected_workflow_raw_sha256_after
                        or current.semantic_sha256
                        != application.expected_workflow_semantic_sha256_after
                        or current.workflow.get("workflow_id") != application.workflow_id):
                    raise StructuralRepairRecoveryError(
                        "stale_workflow",
                        "The editable draft changed after application and cannot be restored automatically.",
                    )
                self.recoveries.append_event(
                    application_id, outcome="pending",
                    reviewer_identity=application.reviewer_identity,
                    fix_session_id=fix_session_id, reason=bounded_reason,
                    current_raw_sha256=current.raw_sha256,
                    current_semantic_sha256=current.semantic_sha256,
                    occurred_at=self.now().isoformat(),
                )
                try:
                    restored = draft.restore(
                        application.expected_workflow_raw_sha256_after,
                        material["original_bytes"],
                    ).after
                except WorkflowDraftPersistenceError as error:
                    self.recoveries.append_event(
                        application_id, outcome="failed",
                        reviewer_identity=application.reviewer_identity,
                        fix_session_id=fix_session_id, reason=bounded_reason,
                        current_raw_sha256=current.raw_sha256,
                        current_semantic_sha256=current.semantic_sha256,
                        occurred_at=self.now().isoformat(),
                    )
                    code = "stale_workflow" if error.code == "stale_workflow" else "recovery_failed"
                    raise StructuralRepairRecoveryError(code, str(error)) from error
                if (restored.raw_sha256 != application.workflow_raw_sha256_before
                        or restored.semantic_sha256
                        != application.workflow_semantic_sha256_before):
                    raise StructuralRepairRecoveryError(
                        "recovery_failed", "Restored draft does not match the original fingerprints."
                    )
                try:
                    event = self.recoveries.append_event(
                        application_id, outcome="recovered",
                        reviewer_identity=application.reviewer_identity,
                        fix_session_id=fix_session_id, reason=bounded_reason,
                        current_raw_sha256=current.raw_sha256,
                        current_semantic_sha256=current.semantic_sha256,
                        restored_raw_sha256=restored.raw_sha256,
                        restored_semantic_sha256=restored.semantic_sha256,
                        occurred_at=self.now().isoformat(),
                    )
                except StructuralRepairRecoveryRepositoryError as error:
                    raise StructuralRepairRecoveryError(
                        "recovery_provenance_failed",
                        "The editable draft was restored, but recovery provenance could not be finalized.",
                    ) from error
        except WorkflowDraftPersistenceError as error:
            code = "lock_unavailable" if error.code == "lock_unavailable" else "recovery_failed"
            raise StructuralRepairRecoveryError(code, str(error)) from error
        return {
            "status": "recovered",
            "application": application.to_dict(),
            "recovery_event": event,
            "workflow": restored.workflow,
        }

    @staticmethod
    def _match_material(application, material: dict[str, Any]) -> None:
        checks = {
            "application_id": application.application_id,
            "approval_id": application.approval_id,
            "task_id": application.task_id,
            "finding_id": application.finding_id,
            "fix_session_id": application.fix_session_id,
            "reviewer_identity": application.reviewer_identity,
            "workflow_id": application.workflow_id,
            "workflow_path": application.workflow_path,
            "workflow_raw_sha256_before": application.workflow_raw_sha256_before,
            "workflow_semantic_sha256_before": application.workflow_semantic_sha256_before,
            "expected_workflow_raw_sha256_after": application.expected_workflow_raw_sha256_after,
            "expected_workflow_semantic_sha256_after": (
                application.expected_workflow_semantic_sha256_after
            ),
        }
        if any(material.get(key) != value for key, value in checks.items()):
            raise StructuralRepairRecoveryError(
                "recovery_invalid", "Recovery material does not match the applied transaction."
            )
