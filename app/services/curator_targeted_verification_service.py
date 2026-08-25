from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.workflow_draft_service import WorkflowDraftService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.curator_resolution_service import CuratorResolutionService
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_identity_resolver import ArticleIdentityResolver
from curator.checks import CuratorChecks
from curator.inventory import CuratorInventory
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

    def relationship_evidence(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """Project current explicit declarations for deterministic relationship tasks."""
        supported = {
            "command_article_relationship_invalid",
            "command_command_relationship_invalid",
            "article_command_reciprocity_conflict",
        }
        if task.get("finding_type") not in supported:
            return None
        inventory = CuratorInventory(self.root).collect()
        commands = {item.identifier: item for item in inventory if item.content_type == "command"}
        published_articles = {
            item.identifier: item for item in inventory
            if item.content_type == "article" and item.state == "published"
        }
        identifier = str(task.get("content_identifier") or "")
        command = commands.get(identifier)
        if not command:
            return {
                "heading": "Current command relationship data",
                "affected_type": "command", "affected_id": identifier,
                "target_found": False, "related_articles": [], "related_commands": [],
                "articles": [], "commands": [],
            }
        raw = command.raw
        related_articles = self._declared_values(raw, "related_articles")
        related_commands = self._declared_values(raw, "related_commands")
        resolver = ArticleIdentityResolver(KnowledgeRepository(self.root / "knowledge_base"))
        article_ids = set(related_articles)
        if task.get("finding_type") == "article_command_reciprocity_conflict":
            for evidence in [*task.get("evidence", []), *task.get("current_evidence", [])]:
                if str(evidence).startswith("Article: "):
                    article_ids.add(str(evidence).split(": ", 1)[1].strip())
            article_ids.update(
                article.identifier for article in published_articles.values()
                if identifier in (article.raw.get("related_commands") or [])
            )
        article_records = []
        for article_id in sorted(article_ids):
            match = resolver.resolve_published(identifier=article_id)
            article = published_articles.get(
                str((match.article.get("canonical_id") or match.article.get("id"))) if match else "")
            article_records.append({
                "id": article_id,
                "found": article is not None,
                "source_path": article.source_path if article else "",
                "title": article.title if article else "",
                "overview": str(article.raw.get("overview") or article.raw.get("summary") or "") if article else "",
                "category": article.category if article else "",
                "tags": self._declared_values(article.raw, "tags") if article else [],
                "structured_commands": self._structured_command_text(article.raw) if article else [],
                "related_commands": self._declared_values(article.raw, "related_commands") if article else [],
                "related_commands_declared": bool(article and "related_commands" in article.raw),
            })
        command_records = [{
            "id": command_id,
            "found": command_id in commands,
            "title": commands[command_id].title if command_id in commands else "",
        } for command_id in related_commands]
        return {
            "heading": "Current relationship declarations",
            "affected_type": "command", "affected_id": identifier,
            "target_found": True, "source_path": command.source_path,
            "related_articles": related_articles,
            "related_articles_declared": "related_articles" in raw,
            "related_commands": related_commands,
            "related_commands_declared": "related_commands" in raw,
            "command_context": {
                "id": identifier,
                "title": str(raw.get("title") or raw.get("name") or identifier),
                "name": str(raw.get("name") or ""),
                "summary": str(raw.get("summary") or ""),
                "category": command.category,
                "platforms": self._metadata_values(raw.get("platforms") or raw.get("platform")),
                "tags": self._declared_values(raw, "tags"),
            },
            "articles": article_records, "commands": command_records,
        }

    @staticmethod
    def _declared_values(record: dict[str, Any], field: str) -> list[str]:
        value = record.get(field)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _metadata_values(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return [str(value)] if str(value or "").strip() else []

    @staticmethod
    def _structured_command_text(article: dict[str, Any]) -> list[str]:
        commands = article.get("commands")
        if not isinstance(commands, list):
            return []
        return [str(item.get("command")).strip() for item in commands
                if isinstance(item, dict) and str(item.get("command") or "").strip()]

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
        relationship_rules = {
            "CUR-REL-CMD-ARTICLE-001",
            "CUR-REL-CMD-COMMAND-001",
            "CUR-REL-ARTICLE-COMMAND-RECIPROCITY-001",
        }
        if task.get("curator_rule") in relationship_rules and task.get("content_type") in {"article", "command"}:
            inventory = CuratorInventory(self.root).collect()
            affected = next((record for record in inventory
                             if record.content_type == task.get("content_type")
                             and record.identifier == task.get("content_identifier")), None)
            if not affected:
                return self._record(task_id, {**base, "status": "not_found",
                    "message": "The affected authoritative content record can no longer be located."})
            findings = self.checks.relationship_findings(inventory)
            exact = [finding for finding in findings
                     if finding.rule == task.get("curator_rule")
                     and finding.content_identifier == task.get("content_identifier")
                     and finding.finding_type == task.get("finding_type")]
            status = "still_detected" if exact else "appears_corrected"
            message = ("The current authoritative content still matches the deterministic relationship condition."
                       if exact else
                       "Curator no longer detects the original deterministic relationship condition. Human resolution remains explicit.")
            return self._record(task_id, {**base, "status": status, "message": message,
                "affected_fingerprint": self.fingerprint(affected.raw),
                "human_approval_required": True})
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
                      "human_approval_required": True,
                      "no_action_required": semantic == "relationship_satisfied",
                      "affected_fingerprint": relationship.get("content_fingerprint", "")}
            recorded = self._record(task_id, result)
            if semantic == "relationship_satisfied":
                CuratorResolutionService(self.root).complete_if_authoritative(task_id)
            return recorded
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

    def _record(self, task_id: str, value: dict[str, Any]) -> dict[str, Any]:
        self.store.record_verification(task_id, value)
        return value
