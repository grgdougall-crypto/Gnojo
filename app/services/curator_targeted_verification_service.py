from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.workflow_draft_service import WorkflowDraftService
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.models import InventoryRecord


class CuratorTargetedVerificationService:
    """Read-only verification of one task against its current affected content."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.drafts_path = self.root / "app" / "workflow_drafts"
        self.checks = CuratorChecks(self.root)

    def _drafts(self) -> WorkflowDraftService | None:
        # Verification is read-only. Never create repository structure merely to
        # discover that affected content is absent.
        return WorkflowDraftService(self.drafts_path) if self.drafts_path.is_dir() else None

    def verify(self, task_id: str) -> dict[str, Any]:
        task = self.store.load().get("tasks", {}).get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        now = datetime.now(timezone.utc).isoformat()
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        base = {
            "verified_at": now,
            "rule": task.get("curator_rule"),
            "workflow_id": workflow_id,
            "node_id": node_id,
            "status": "not_automatable",
            "message": "Curator cannot safely verify this task from one affected workflow node.",
            "human_approval_required": True,
        }
        if task.get("content_type") not in {"workflow", "workflow_node"}:
            return self._record(task_id, base)
        drafts = self._drafts()
        if drafts is None:
            return self._record(task_id, {**base, "status": "not_found",
                "message": "The affected workflow can no longer be located."})
        draft = next((item for item in drafts.list_drafts()
                      if item.get("workflow_id") == workflow_id and not item.get("is_damaged")), None)
        if not draft:
            return self._record(task_id, {**base, "status": "not_found",
                "message": "The affected workflow can no longer be located."})
        workflow = drafts.get_draft(draft["filename"])
        node = (workflow or {}).get("nodes", {}).get(node_id) if node_id else None
        if node_id and not isinstance(node, dict):
            return self._record(task_id, {**base, "status": "not_found",
                "message": "The affected node can no longer be located in the current workflow."})
        affected = node if isinstance(node, dict) else workflow
        fingerprint = self.fingerprint(affected)
        record = InventoryRecord(
            "workflow", workflow_id, str(workflow.get("name") or workflow_id),
            str(self.root / "app" / "workflow_drafts" / draft["filename"]),
            str(workflow.get("category") or ""), str(workflow.get("platform") or ""),
            "draft", workflow,
        )
        findings = self.checks.run_record(record)
        exact = [finding for finding in findings
                 if finding.rule == task.get("curator_rule")
                 and finding.content_identifier == task.get("content_identifier")
                 and finding.finding_type == task.get("finding_type")]
        rule = str(task.get("curator_rule") or "")
        supported_rule = rule.startswith(("CUR-SAFE-", "CUR-META-", "CUR-TAX-", "CUR-CONTENT-",
                                           "GNOJO-WORKFLOW-"))
        if exact:
            result = {**base, "status": "still_detected",
                      "message": "The current workflow content still matches the Curator condition.",
                      "affected_fingerprint": fingerprint}
        elif not supported_rule:
            result = {**base, "status": "human_review_required",
                      "message": "This rule does not have a trusted targeted verifier. Review the current content manually.",
                      "affected_fingerprint": fingerprint}
        elif str(task.get("classification") or "").casefold() in {"risk", "opportunity", "recommendation"}:
            result = {**base, "status": "appears_corrected",
                      "message": "Curator no longer detects the original condition in the current workflow content. Human approval is still required.",
                      "affected_fingerprint": fingerprint}
        else:
            result = {**base, "status": "appears_corrected",
                      "message": "Curator no longer detects the original deterministic condition in the current workflow content. Resolution remains explicit.",
                      "affected_fingerprint": fingerprint}
        return self._record(task_id, result)

    def current_fingerprint(self, task: dict[str, Any]) -> str:
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        drafts = self._drafts()
        if drafts is None:
            return ""
        draft = next((item for item in drafts.list_drafts()
                      if item.get("workflow_id") == workflow_id and not item.get("is_damaged")), None)
        if not draft:
            return ""
        workflow = drafts.get_draft(draft["filename"]) or {}
        affected = workflow.get("nodes", {}).get(node_id) if node_id else workflow
        return self.fingerprint(affected) if isinstance(affected, dict) else ""

    @staticmethod
    def fingerprint(value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _record(self, task_id: str, value: dict[str, Any]) -> dict[str, Any]:
        self.store.record_verification(task_id, value)
        return value
