from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.workflow_draft_service import WorkflowDraftService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.models import InventoryRecord
from curator.resolution import ResolutionPackageError, ResolutionPackageRepository


class CuratorTargetedVerificationService:
    """Read-only verification of one task against its current affected content."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.drafts_path = self.root / "app" / "workflow_drafts"
        self.checks = CuratorChecks(self.root)
        self.lifecycle = CuratorWorkflowLifecycleService(self.root)

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
        if task.get("curator_rule") == "CUR-REL-ARTICLE-CANDIDATE":
            relationship = self.lifecycle.relationship(workflow_id, node_id)
            semantic = relationship.get("status", "target_unavailable")
            expected = self._expected_canonical_article(task_id)
            if (semantic == "relationship_satisfied" and expected
                    and relationship.get("canonical_article_id") != expected):
                semantic = "relationship_conflict_or_unresolved"
                relationship["expected_canonical_article_id"] = expected
            messages = {
                "relationship_missing": "The authoritative workflow node still has no knowledge article relationship.",
                "relationship_satisfied": "The authoritative workflow node already links to a canonical published article; no repair is required.",
                "relationship_conflict_or_unresolved": "The current relationship is not resolvable as a canonical published article and requires human review.",
                "target_unavailable": "The authoritative workflow or affected node could not be located.",
            }
            result = {**base, **{key: value for key, value in relationship.items() if key != "node"},
                      "status": semantic, "message": messages[semantic],
                      "human_approval_required": semantic != "relationship_satisfied",
                      "no_action_required": semantic == "relationship_satisfied",
                      "affected_fingerprint": relationship.get("content_fingerprint", "")}
            return self._record(task_id, result, reconcile_satisfied=True)
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

    def _expected_canonical_article(self, task_id: str) -> str:
        """Read a package's reviewed identity expectation without creating one."""
        try:
            package = ResolutionPackageRepository(self.root / "curation_memory").get(task_id) or {}
        except ResolutionPackageError:
            return ""
        values = (
            package.get("canonical_recommendation"),
            package.get("proposed_article_id"),
            (package.get("identity_resolution") or {}).get("canonical_article_id"),
        )
        normalized = {str(value).strip() for value in values if str(value or "").strip()}
        return next(iter(normalized)) if len(normalized) == 1 else ""

    def current_fingerprint(self, task: dict[str, Any]) -> str:
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        target = self.lifecycle.resolve(workflow_id)
        if not target:
            return ""
        workflow = target.workflow
        affected = workflow.get("nodes", {}).get(node_id) if node_id else workflow
        return self.fingerprint(affected) if isinstance(affected, dict) else ""

    @staticmethod
    def fingerprint(value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _record(self, task_id: str, value: dict[str, Any], *, reconcile_satisfied: bool = False) -> dict[str, Any]:
        self.store.record_verification(task_id, value)
        if reconcile_satisfied and value.get("status") == "relationship_satisfied":
            task = self.store.load().get("tasks", {}).get(task_id, {})
            if task.get("status") not in {"resolved", "ignored", "superseded"}:
                self.store.update_task(
                    task_id, status="resolved", actor="Curator targeted verification",
                    note="Relationship already satisfied on authoritative lifecycle copy; no repair was performed.",
                    event_name="relationship_satisfied_no_action_required",
                    metadata={"resolution_kind": "no_action_required", "repair_performed": False,
                              "source_path": value.get("source_path"), "lifecycle": value.get("lifecycle")},
                )
        return value
