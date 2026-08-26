from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.structural_repair_approval_repository import (
    StructuralRepairApprovalRepository,
)
from app.services.curator_structural_repair_contracts import (
    ProgressMetadataRepairPlan,
    StructuralRepairPlan,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_task_service import CuratorTaskService
from app.services.curator_structural_repair_governance import (
    STAGE3_SCHEMA_VERSION,
    StructuralRepairApproval,
    StructuralRepairFingerprint,
)
from app.services.workflow_draft_persistence import WorkflowDraftPersistence


class CuratorStructuralRepairApprovalService:
    """Issue one bounded server-owned approval from a validated structural preview."""

    DEFAULT_LIFETIME = timedelta(minutes=15)
    MAXIMUM_LIFETIME = timedelta(minutes=30)

    def __init__(self, repository_root: Path):
        self.root = Path(repository_root).resolve()
        self.repository = StructuralRepairApprovalRepository(self.root / "curation_memory")
        self.now = lambda: datetime.now(timezone.utc)
        self.task_loader = lambda task_id: CuratorTaskService(self.root).get(task_id)
        self.registry = CuratorRepairAdapterRegistry()
        self.persistence = WorkflowDraftPersistence(self.root / "app" / "workflow_drafts")

    @classmethod
    def _for_test(cls, repository_root: Path, *, task_loader: Callable[[str], dict[str, Any]],
                  registry: CuratorRepairAdapterRegistry | None = None,
                  now: Callable[[], datetime] | None = None):
        """Explicit test-only construction without broadening the production constructor."""
        value = cls.__new__(cls)
        value.root = Path(repository_root).resolve()
        value.repository = StructuralRepairApprovalRepository(value.root / "curation_memory")
        value.now = now or (lambda: datetime.now(timezone.utc))
        value.task_loader = task_loader
        value.registry = registry or CuratorRepairAdapterRegistry()
        value.persistence = WorkflowDraftPersistence(value.root / "app" / "workflow_drafts")
        return value

    def issue(self, *, task_id: str, workflow_filename: str, reviewer_identity: str,
              fix_session_id: str, lifetime: timedelta | None = None) -> StructuralRepairApproval:
        requested_lifetime = self.DEFAULT_LIFETIME if lifetime is None else lifetime
        if (not isinstance(requested_lifetime, timedelta)
                or requested_lifetime <= timedelta(0)
                or requested_lifetime > self.MAXIMUM_LIFETIME):
            raise ValueError("Approval lifetime must be positive and no more than 30 minutes.")
        task = self.task_loader(str(task_id or "").strip())
        if task.get("status", "open") not in {"open", "in_progress"}:
            raise ValueError("The originating task is not actionable.")
        registration = self.registry.lookup(task.get("curator_rule", ""), task.get("finding_type", ""))
        if not registration or not registration.structural or not registration.can_preview:
            raise ValueError("No governed structural capability matches this task.")
        with self.persistence.locked(workflow_filename) as draft:
            snapshot = draft.read()
        if snapshot.filename != f"{snapshot.workflow.get('workflow_id')}.json":
            raise ValueError("Editable workflow filename and identity do not match.")
        preview = self.registry.preview(
            task, snapshot.workflow,
            workflow_raw_sha256=snapshot.raw_sha256,
            workflow_semantic_sha256=snapshot.semantic_sha256,
        )
        if not preview.get("available") or not preview.get("read_only"):
            raise ValueError("A valid governed structural preview is required.")
        raw_plan = preview.get("plan")
        plan = (ProgressMetadataRepairPlan.from_dict(raw_plan)
                if isinstance(raw_plan, dict) and raw_plan.get("plan_type") == "workflow_metadata"
                else StructuralRepairPlan.from_dict(raw_plan))
        if plan.workflow_id != snapshot.workflow.get("workflow_id"):
            raise ValueError("Preview and editable workflow identities do not match.")
        specification = preview.get("specification")
        if not isinstance(specification, dict):
            raise ValueError("The preview must contain its complete evidence specification.")
        created = self.now()
        expires = created + requested_lifetime
        approval = StructuralRepairApproval.from_dict({
            "schema_version": STAGE3_SCHEMA_VERSION,
            "approval_id": "SRA-" + secrets.token_hex(8).upper(),
            "application_id": "SRX-" + secrets.token_hex(8).upper(),
            "task_id": task.get("task_id"), "finding_id": task.get("finding_id", ""),
            "fix_session_id": fix_session_id, "reviewer_identity": reviewer_identity,
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": plan.workflow_id, "workflow_filename": snapshot.filename,
            "workflow_lifecycle": "draft",
            "workflow_path": f"app/workflow_drafts/{snapshot.filename}",
            "workflow_raw_sha256_before": snapshot.raw_sha256,
            "workflow_semantic_sha256_before": snapshot.semantic_sha256,
            "adapter_id": registration.adapter_id, "plan_id": plan.plan_id,
            "plan_digest": StructuralRepairFingerprint.contract(preview["plan"]),
            "specification_id": specification.get("specification_id"),
            "specification_version": specification.get("version"),
            "specification_digest": StructuralRepairFingerprint.contract(specification),
            "preview_digest": StructuralRepairFingerprint.contract(preview),
            "created_at": created.isoformat(), "expires_at": expires.isoformat(),
            "approval_state": "approved",
        })
        self.repository.issue(approval, preview)
        return approval
