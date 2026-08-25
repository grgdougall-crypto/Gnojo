from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.structural_repair_approval_repository import (
    StructuralRepairApprovalRepository,
)
from app.services.curator_structural_repair_contracts import StructuralRepairPlan
from app.services.curator_structural_repair_governance import (
    STAGE3_SCHEMA_VERSION,
    StructuralRepairApproval,
    StructuralRepairFingerprint,
)
from app.services.workflow_draft_persistence import WorkflowDraftSnapshot


class CuratorStructuralRepairApprovalService:
    """Issue one bounded server-owned approval from a validated structural preview."""

    def __init__(self, repository_root: Path, *, now: Callable[[], datetime] | None = None):
        self.root = Path(repository_root).resolve()
        self.repository = StructuralRepairApprovalRepository(self.root / "curation_memory")
        self.now = now or (lambda: datetime.now(timezone.utc))

    def issue(self, *, task: dict[str, Any], preview: dict[str, Any],
              snapshot: WorkflowDraftSnapshot, reviewer_identity: str,
              fix_session_id: str, adapter_id: str,
              lifetime: timedelta = timedelta(minutes=30)) -> StructuralRepairApproval:
        if not preview.get("available") or not preview.get("read_only"):
            raise ValueError("A valid governed structural preview is required.")
        plan = StructuralRepairPlan.from_dict(preview.get("plan"))
        if plan.workflow_id != snapshot.workflow.get("workflow_id"):
            raise ValueError("Preview and editable workflow identities do not match.")
        specification = preview.get("specification")
        if not isinstance(specification, dict):
            raise ValueError("The preview must contain its complete evidence specification.")
        created = self.now()
        expires = created + lifetime
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
            "adapter_id": adapter_id, "plan_id": plan.plan_id,
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
