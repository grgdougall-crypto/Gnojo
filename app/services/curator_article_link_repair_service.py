from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import ArticleNotFoundError, KnowledgeRepository
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.knowledge_identity_service import KnowledgeIdentityError, KnowledgeIdentityService
from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from app.services.workflow_validation_service import WorkflowValidationService
from app.services.workflow_draft_persistence import (
    WorkflowDraftPersistence,
    WorkflowDraftPersistenceError,
)
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from curator.memory import CuratorMemoryStore
from curator.resolution import ResolutionPackageRepository


class CuratorArticleLinkRepairError(RuntimeError):
    """A trusted relationship repair could not be proven or completed."""


class CuratorArticleLinkRepairService:
    """Approval-only adapter for one exact canonical article relationship rule."""

    REGISTERED_RULES = {"CUR-REL-ARTICLE-CANDIDATE"}

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.packages = ResolutionPackageRepository(self.root / "curation_memory")
        self.knowledge = KnowledgeRepository(self.root / "knowledge_base")
        self.sessions = CuratorFixSessionService(self.root)

    def preview(self, task_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "available": False, "eligible": False, "already_satisfied": False,
            "blocking_reason": "", "validation": {"passed": False, "errors": []},
            "what_changes": "No trusted content will change from this preview.",
            "what_does_not_change": (
                "Workflow and node identity, instruction/help text, unrelated relationships, "
                "article content/history, and all publication states remain unchanged."
            ),
        }
        task = self.memory.load().get("tasks", {}).get(task_id)
        if not task:
            return self._block(result, "Knowledge Task was not found.")
        if task.get("curator_rule") not in self.REGISTERED_RULES:
            return self._block(result, "No trusted adapter is registered for this Curator rule.")
        package = self.packages.get(task_id)
        if not package:
            return self._block(result, "Assisted Resolution Package was not found.")
        if package.get("recommendation") != "LINK_EXISTING_ARTICLE":
            return self._block(result, "The package does not recommend Link Existing Article.")
        proposal = package.get("proposed_relationship") or {}
        if proposal.get("action") != "RELINK_EXISTING":
            return self._block(result, "The package relationship action is not eligible for this adapter.")

        workflow_id = str(proposal.get("workflow_id") or "").strip()
        node_id = str(proposal.get("node_id") or "").strip()
        target = str(proposal.get("target_article_id") or "").strip()
        result.update({"workflow_id": workflow_id, "node_id": node_id,
                       "proposed_article_id": target})
        if task.get("content_type") != "workflow_node" or task.get("content_identifier") != f"{workflow_id}:{node_id}":
            return self._block(result, "The task and package target identities do not match.")
        if package.get("workflow_id") != workflow_id or package.get("node_id") != node_id:
            return self._block(result, "The package contains conflicting workflow or node identities.")
        identities = {
            str(package.get("proposed_article_id") or ""),
            str(package.get("canonical_recommendation") or ""),
            str((package.get("identity_resolution") or {}).get("canonical_article_id") or ""),
            target,
        }
        if len(identities) != 1 or not target:
            return self._block(result, "The package contains conflicting canonical article identities.")
        if (package.get("identity_resolution") or {}).get("status") != "matched":
            return self._block(result, "The package did not prove a canonical article match.")

        try:
            article = self.knowledge.get_published_article(target)
            canonical = KnowledgeIdentityService.canonical_id(article)
        except (ArticleNotFoundError, KnowledgeIdentityError):
            return self._block(result, "The proposed canonical article is not published or no longer exists.")
        if canonical != target or article.get("id") != target:
            return self._block(result, "The published article identity does not match the package.")
        review = article.get("review") or {}
        if str(review.get("status") or "").casefold() != "approved":
            return self._block(result, "The proposed article is not approved for publication.")
        result.update({"proposed_article_title": article.get("title", target),
                       "article_review_state": review.get("status"), "article_publication_state": "published"})

        filename = str(package.get("workflow_filename") or f"{workflow_id}.json")
        if Path(filename).name != filename:
            return self._block(result, "The workflow filename is invalid.")
        workflow_path = self.root / "app" / "workflow_drafts" / filename
        if not workflow_path.exists():
            return self._block(result, "The workflow draft no longer exists.")
        try:
            workflow_bytes = workflow_path.read_bytes()
            workflow = json.loads(workflow_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._block(result, "The workflow draft cannot be read safely.")
        if workflow.get("workflow_id") != workflow_id:
            return self._block(result, "The workflow identity no longer matches the package.")
        node = (workflow.get("nodes") or {}).get(node_id)
        if not isinstance(node, dict):
            return self._block(result, "The target workflow node no longer exists.")
        result["node_title"] = node.get("title") or node.get("question") or node.get("name") or node_id
        current = node.get("knowledge_article")
        if current is not None and not isinstance(current, str):
            return self._block(result, "The workflow relationship has an unsupported structure.")
        current = str(current or "").strip()
        if "current_relationship" in package:
            package_current = package.get("current_relationship")
            if package_current is not None and not isinstance(package_current, str):
                return self._block(result, "The package's recorded relationship has an unsupported structure.")
            if str(package_current or "").strip() != current:
                return self._block(result, "The package no longer describes the workflow's current relationship.")
        result.update({"current_relationships": [current] if current else [],
                       "before": {"knowledge_article": current or None},
                       "after": {"knowledge_article": target},
                       "relationship_already_exists": current == target})

        proposed = deepcopy(workflow)
        proposed["nodes"][node_id]["knowledge_article"] = target
        validation = WorkflowValidationService().validate(proposed)
        errors = list(validation.get("errors") or [])
        result["validation"] = {"passed": not errors, "errors": errors}
        result["preview_token"] = self._fingerprint(task, package, workflow_bytes, article)
        result["available"] = True
        if current == target:
            result["already_satisfied"] = True
            result["blocking_reason"] = "This canonical article is already linked; no repair is necessary."
            result["what_changes"] = "Nothing. The relationship is already satisfied."
            return result
        if current:
            return self._block(result, "The node is linked to a different article; this adapter never replaces relationships.")
        if errors:
            return self._block(result, "The proposed workflow does not pass validation.")
        result["eligible"] = True
        result["what_changes"] = f"Set this node's knowledge_article relationship to '{target}'."
        return result

    def apply(self, task_id: str, *, session_id: str, preview_token: str,
              approved: bool) -> dict[str, Any]:
        if not approved:
            raise CuratorArticleLinkRepairError("Explicit reviewer approval is required.")
        session = self.sessions.get(session_id)
        reviewer = str(session.get("started_by") or "").strip()
        if not reviewer:
            raise CuratorArticleLinkRepairError("The maintenance session has no reviewer identity.")
        matches = [item for item in session.get("repair_queue", [])
                   if item.get("status", "open") == "open"
                   and str((item.get("affected_content") or {}).get("task_id") or "") == task_id]
        if len(matches) != 1:
            raise CuratorArticleLinkRepairError("This task is not one unique open item in the maintenance session.")
        preview = self.preview(task_id)
        if preview.get("preview_token") != preview_token:
            raise CuratorArticleLinkRepairError("Repository state changed. Refresh and review the repair again.")
        if preview.get("already_satisfied"):
            raise CuratorArticleLinkRepairError("The relationship is already satisfied; no repair was recorded.")
        if not preview.get("eligible"):
            raise CuratorArticleLinkRepairError(preview.get("blocking_reason") or "Repair is not eligible.")

        package = self.packages.get(task_id) or {}
        workflow_path = self.root / "app" / "workflow_drafts" / package["workflow_filename"]
        tracked = [workflow_path, self.memory.state_path,
                   self.root / "curation_memory" / "fix_sessions" / f"{session_id}.json",
                   self.root / "curation_memory" / "resolution_packages" / f"{task_id}.json"]
        snapshots = {path: path.read_bytes() if path.exists() else None for path in tracked}
        persisted_replacement = None
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            node = workflow["nodes"][preview["node_id"]]
            if node.get("knowledge_article"):
                raise CuratorArticleLinkRepairError("The workflow relationship changed before apply.")
            node["knowledge_article"] = preview["proposed_article_id"]
            errors = WorkflowValidationService().validate(workflow).get("errors", [])
            if errors:
                raise CuratorArticleLinkRepairError("Resulting workflow failed validation: " + "; ".join(errors))
            persisted_replacement = WorkflowDraftPersistence(workflow_path.parent).compare_and_swap(
                workflow_path.name,
                StructuralRepairFingerprint.raw_workflow(snapshots[workflow_path]),
                workflow,
            )
            verification = CuratorRepairRelationshipVerifier(self.root).verify(
                package["workflow_filename"], preview["node_id"], preview["proposed_article_id"])
            if not verification["verified"]:
                raise CuratorArticleLinkRepairError("Persisted relationship verification failed.")
            self.packages.record_event(
                task_id, "canonical_article_relationship_linked", actor=reviewer,
                maintenance_session_id=session_id, workflow_id=preview["workflow_id"],
                node_id=preview["node_id"], article_id=preview["proposed_article_id"],
            )
            self.memory.update_task(
                task_id, status="resolved", actor=reviewer,
                event_name="deterministic_relationship_repair",
                note="Linked the approved canonical article to the verified workflow node.",
                metadata={"maintenance_session_id": session_id, "adapter": "canonical_article_link",
                          "workflow_id": preview["workflow_id"], "node_id": preview["node_id"],
                          "article_id": preview["proposed_article_id"]},
            )
            current = KnowledgeIntegrityService(self.root).report()
            updated = self.sessions.record(
                session_id, matches[0]["item_id"], "completed",
                note="Approved canonical article relationship linked.",
                verification={**verification, "actor": reviewer, "task_id": task_id}, current=current,
            )
        except Exception:
            for path, content in snapshots.items():
                if content is None:
                    path.unlink(missing_ok=True)
                elif path == workflow_path and persisted_replacement is not None:
                    try:
                        WorkflowDraftPersistence(workflow_path.parent).compare_and_swap(
                            workflow_path.name, persisted_replacement.after.raw_sha256, content,
                        )
                    except WorkflowDraftPersistenceError:
                        pass
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            raise
        return {"applied": True, "preview": preview, "session": updated,
                "next_item_id": next((item["item_id"] for item in updated["repair_queue"]
                                      if item.get("status", "open") == "open"), "")}

    @staticmethod
    def _block(result: dict[str, Any], reason: str) -> dict[str, Any]:
        result["eligible"] = False
        result["blocking_reason"] = reason
        return result

    @staticmethod
    def _fingerprint(task: dict[str, Any], package: dict[str, Any], workflow_bytes: bytes,
                     article: dict[str, Any]) -> str:
        value = {"task": {key: task.get(key) for key in ("task_id", "status", "curator_rule", "content_identifier")},
                 "package": {key: package.get(key) for key in ("version", "recommendation", "proposed_relationship",
                                                                 "proposed_article_id", "canonical_recommendation")},
                 "workflow_sha256": hashlib.sha256(workflow_bytes).hexdigest(), "article": article}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)


class CuratorRepairRelationshipVerifier:
    def __init__(self, root: Path):
        self.root = root

    def verify(self, filename: str, node_id: str, article_id: str) -> dict[str, Any]:
        try:
            workflow = json.loads((self.root / "app" / "workflow_drafts" / filename).read_text(encoding="utf-8"))
            article = KnowledgeRepository(self.root / "knowledge_base").get_published_article(article_id)
            errors = WorkflowValidationService().validate(workflow).get("errors", [])
            linked = workflow["nodes"][node_id].get("knowledge_article") == article_id
            approved = str((article.get("review") or {}).get("status") or "").casefold() == "approved"
            return {"verified": linked and approved and not errors, "relationship_verified": linked,
                    "article_approved": approved, "workflow_valid": not errors, "errors": errors}
        except (OSError, KeyError, json.JSONDecodeError, ArticleNotFoundError):
            return {"verified": False, "errors": ["Persisted relationship could not be verified."]}
