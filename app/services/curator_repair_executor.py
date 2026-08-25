from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.curator_repair_validator import CuratorRepairValidator
from app.services.curator_task_reconciliation_service import CuratorTaskReconciliationService
from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from app.services.workflow_draft_persistence import WorkflowDraftPersistence, WorkflowDraftPersistenceError
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from curator.governance import CuratorGovernancePolicy
from curator.memory import CuratorMemoryStore


class CuratorRepairError(RuntimeError):
    pass


class CuratorRepairExecutor:
    """Trusted adapters for deterministic, reversible, non-editorial repairs."""

    ALLOWED = {"RELINK_EXISTING", "REBUILD_INVENTORY"}

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.integrity = KnowledgeIntegrityService(self.root)
        self.validator = CuratorRepairValidator(self.root)
        self.reconciler = CuratorTaskReconciliationService(self.root)

    def preview(self, item: dict[str, Any]) -> dict[str, Any]:
        state = CuratorMemoryStore(self.root / "curation_memory").load()
        CuratorGovernancePolicy.authorize("repair_preview", "preview_repairs", state["controls"])
        if item.get("classification") not in self.ALLOWED or not item.get("safe_automatic"):
            raise CuratorRepairError("This finding requires a human decision and is not eligible for automatic repair.")
        return {"item_id": item["item_id"], "classification": item["classification"],
                "affected_content": item["affected_content"], "what_will_change": item["what_will_change"],
                "what_will_not_change": item["what_will_not_change"],
                "rollback": "The original file is restored automatically if validation or verification fails."}

    def apply(self, item: dict[str, Any], *, session_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise CuratorRepairError("Review and confirm the proposed repair before applying it.")
        self.preview(item)
        state = CuratorMemoryStore(self.root / "curation_memory").load()
        CuratorGovernancePolicy.authorize("approved_repair", "execute_approved_repairs", state["controls"])
        if item["classification"] == "REBUILD_INVENTORY":
            path = self.root / "knowledge_base" / "inventory.json"
            before = path.read_bytes() if path.exists() else None
            try:
                self.integrity.rebuild_index()
                verification = self.validator.inventory()
                if not verification["verified"]:
                    raise CuratorRepairError("The rebuilt inventory did not pass targeted verification.")
            except Exception:
                self._restore(path, before)
                raise
        else:
            verification = self._relink(item)
        tasks = self.reconciler.reconcile(item, session_id=session_id, verified=verification["verified"])
        return {"verification": verification, "reconciled_tasks": tasks,
                "current_integrity": self.integrity.report()}

    def approve_legacy_validation(self, item: dict[str, Any], *, session_id: str,
                                  reviewer: str, confirmed: bool) -> dict[str, Any]:
        if item.get("classification") != "LEGACY_REVIEW_REQUIRED" or not confirmed:
            raise CuratorRepairError("A confirmed legacy validation is required.")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise CuratorRepairError("A current reviewer identity is required.")
        article_id = item["affected_content"]["id"]
        article = deepcopy(self.integrity.repository.get_published_article(article_id))
        before = deepcopy(article)
        now = datetime.now(timezone.utc).isoformat()
        article["review"] = {
            **(article.get("review") or {}), "status": "approved", "reviewed_by": reviewer,
            "reviewed_at": now, "review_type": "legacy_validation",
            "original_historical_reviewer": "unknown",
        }
        try:
            self.integrity.repository.save_published(article, overwrite=True)
            current = self.integrity.report()
            remaining = {entry["id"] for entry in current["missing_review_metadata"]}
            verification = {"verified": article_id not in remaining, "reviewed_at": now,
                            "review_type": "legacy_validation"}
            if not verification["verified"]:
                raise CuratorRepairError("Legacy provenance verification failed.")
            self.integrity.rebuild_index()
        except Exception:
            self.integrity.repository.save_published(before, overwrite=True)
            raise
        tasks = self.reconciler.reconcile(item, session_id=session_id, verified=True)
        return {"verification": verification, "reconciled_tasks": tasks, "current_integrity": current}

    def _relink(self, item: dict[str, Any]) -> dict[str, Any]:
        evidence = item["affected_content"]
        path = (self.root / evidence["source"]).resolve()
        if self.root not in path.parents or not path.is_file():
            raise CuratorRepairError("Affected workflow path is invalid.")
        before = path.read_bytes()
        backup = self.root / "curation_memory" / "repair_backups" / item["item_id"] / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(before)
        draft_root = (self.root / "app" / "workflow_drafts").resolve()
        draft_replacement = None
        try:
            workflow = json.loads(before.decode("utf-8"))
            node = (workflow.get("nodes") or {}).get(evidence["node"])
            if not isinstance(node, dict) or node.get("knowledge_article") != evidence["current_reference"]:
                raise CuratorRepairError("This finding is stale. The workflow changed after the session was planned.")
            match = self.integrity.identities.resolve_published(evidence["current_reference"],
                                                                 {"title": evidence["current_reference"]})
            if not match or match.confidence != 1.0 or match.article["id"] != evidence["canonical_target"]:
                raise CuratorRepairError("Canonical identity no longer resolves exactly; no change was applied.")
            node["knowledge_article"] = evidence["canonical_target"]
            if path.parent == draft_root:
                draft_replacement = WorkflowDraftPersistence(draft_root).compare_and_swap(
                    path.name, StructuralRepairFingerprint.raw_workflow(before), workflow,
                )
            else:
                self._write_atomic(path, workflow)
            verification = self.validator.relationship(item)
            if not verification["verified"]:
                raise CuratorRepairError("Targeted verification failed; the original workflow was restored.")
            self.integrity.rebuild_index()
            verification["backup"] = str(backup.relative_to(self.root)).replace("\\", "/")
            return verification
        except Exception:
            if draft_replacement is not None:
                try:
                    WorkflowDraftPersistence(draft_root).compare_and_swap(
                        path.name, draft_replacement.after.raw_sha256, before,
                    )
                except WorkflowDraftPersistenceError:
                    pass
            else:
                self._restore(path, before)
            raise

    @staticmethod
    def _write_atomic(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _restore(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
