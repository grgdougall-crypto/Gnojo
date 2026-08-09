from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_metadata_service import workflow_category, workflow_platform
from app.services.article_identity_resolver import ArticleIdentityResolver


CREATE_NEW_ARTICLE = "CREATE_NEW_ARTICLE"
LINK_EXISTING_ARTICLE = "LINK_EXISTING_ARTICLE"
KEEP_INLINE_ONLY = "KEEP_INLINE_ONLY"


class ArticleCandidateAnalyzer:
    """Resolve a Curator task to content and recommend a reviewable editorial action."""

    ELIGIBLE_TYPES = {"article_candidate", "malformed_relationship", "duplicate_knowledge_candidate"}

    def __init__(self, repository_root: Path):
        self.root = repository_root.resolve()
        self.workflows = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        self.knowledge = KnowledgeRepository(self.root / "knowledge_base")
        self.identities = ArticleIdentityResolver(self.knowledge)

    def analyze(self, task: dict[str, Any]) -> dict[str, Any]:
        if task.get("finding_type") not in self.ELIGIBLE_TYPES:
            raise ValueError("This Knowledge Task is not eligible for assisted resolution.")
        workflow, filename, node_id, node = self._resolve_node(task)
        existing = self.knowledge.get_drafts() + self.knowledge.get_published()
        missing_identifier = task.get("evidence", [None])[0] if task.get("finding_type") in {"malformed_relationship", "duplicate_knowledge_candidate"} else None
        article_title = self._article_title(node, missing_identifier)
        candidate = {"title": article_title, "overview": self._instruction(node),
                     "checklist": [self._instruction(node)] if self._instruction(node) else []}
        identity_probe = missing_identifier or node.get("knowledge_article")
        # Published canonical knowledge always wins. Drafts are consulted only after
        # the canonical index has returned no match.
        match = self.identities.resolve_published(identity_probe, candidate)
        identity_scope = "canonical_published_index"
        if not match:
            match = self.identities.resolve(identity_probe, candidate, include_drafts=True)
            identity_scope = "draft_and_published_index" if match else "all_live_indexes"
        exact = match.article if match else None
        if exact:
            recommendation = LINK_EXISTING_ARTICLE
            article_id = exact["id"]
            article_title = exact.get("title") or self._article_title(node)
            reason = "A matching article already exists; reuse avoids duplicate knowledge."
        else:
            recommendation = CREATE_NEW_ARTICLE
            base = self._slug(missing_identifier or article_title)
            article_id = self._stable_id(base, existing, workflow.get("workflow_id"), node_id)
            reason = "The instruction is reusable and no matching draft or published article was found."
        instruction = self._instruction(node)
        category = workflow_category(workflow)
        platform = workflow_platform(workflow)
        return {
            "workflow_id": workflow.get("workflow_id"), "workflow_filename": filename,
            "workflow_name": workflow.get("name") or workflow.get("workflow_id"),
            "node_id": node_id, "node_type": node.get("type", "instruction"),
            "node_title": node.get("title") or node.get("question") or node_id.replace("_", " ").title(),
            "instruction": instruction, "recommendation": recommendation, "recommendation_reason": reason,
            "proposed_article_id": article_id, "proposed_article_title": article_title,
            "platform": platform, "category": category, "subcategory": self._subcategory(node, category),
            "current_relationship": node.get("knowledge_article"),
            "canonical_recommendation": exact.get("id") if exact else None,
            "duplicate_confidence": round(match.confidence * 100, 1) if match else 0,
            "similarity_score": match.confidence if match else 0,
            "identity_method": match.method if match else None,
            "identity_reasoning": match.reasoning if match else [],
            "identity_resolution": {
                "status": "matched" if match else "no_match",
                "scope": identity_scope,
                "canonical_article_id": exact.get("id") if exact else None,
                "method": match.method if match else None,
                "confidence": round(match.confidence * 100, 1) if match else 0,
            },
        }

    def _resolve_node(self, task: dict[str, Any]):
        content = str(task.get("content_identifier", ""))
        workflow_hint, _, node_hint = content.partition(":")
        missing = task.get("evidence", [None])[0] if task.get("finding_type") in {"malformed_relationship", "duplicate_knowledge_candidate"} else None
        for item in self.workflows.list_drafts():
            if item.get("is_damaged"):
                continue
            filename = item["filename"]
            workflow = self.workflows.get_draft(filename)
            if workflow.get("workflow_id") != workflow_hint:
                continue
            nodes = workflow.get("nodes", {})
            if node_hint and node_hint in nodes:
                return workflow, filename, node_hint, nodes[node_hint]
            matches = [(node_id, node) for node_id, node in nodes.items() if node.get("knowledge_article") == missing]
            if len(matches) == 1:
                node_id, node = matches[0]
                return workflow, filename, node_id, node
        for path in sorted((self.root / "app" / "decision_trees").glob("*.json")):
            try:
                import json
                workflow = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if workflow.get("workflow_id") != workflow_hint:
                continue
            nodes = workflow.get("nodes", {})
            if node_hint and node_hint in nodes:
                return workflow, "", node_hint, nodes[node_hint]
            matches = [(node_id, node) for node_id, node in nodes.items() if node.get("knowledge_article") == missing]
            if len(matches) == 1:
                node_id, node = matches[0]
                return workflow, "", node_id, node
        raise ValueError("The affected workflow node could not be resolved unambiguously.")

    @staticmethod
    def _find_existing(articles, node, missing_identifier):
        linked = node.get("knowledge_article")
        candidates = {str(value).strip().lower() for value in (linked, missing_identifier) if value}
        title = ArticleCandidateAnalyzer._article_title(node).lower()
        for article in articles:
            if str(article.get("id", "")).lower() in candidates or str(article.get("title", "")).lower() == title:
                return article
        return None

    @staticmethod
    def _article_title(node, missing_identifier=None):
        if missing_identifier and " " in str(missing_identifier):
            return str(missing_identifier).strip()
        title = node.get("title") or node.get("question") or "Troubleshooting Step"
        return title if str(title).lower().startswith("how to ") else f"How to {title}"

    @staticmethod
    def _instruction(node):
        return str(node.get("instruction") or node.get("message") or node.get("help_text") or "").strip()

    @staticmethod
    def _slug(value):
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(value).lower())).strip("-")[:80]

    @staticmethod
    def _stable_id(proposed, articles, workflow_id, node_id):
        existing = {article.get("id") for article in articles}
        if proposed not in existing:
            return proposed
        digest = hashlib.sha256(f"{workflow_id}:{node_id}".encode()).hexdigest()[:8]
        return f"{proposed[:71]}-{digest}"

    @staticmethod
    def _subcategory(node, category):
        text = f"{node.get('title','')} {ArticleCandidateAnalyzer._instruction(node)}".lower()
        for needle, label in (("vpn", "VPN"), ("printer", "Printing"), ("monitor", "Displays"), ("audio", "Audio"), ("storage", "Storage"), ("update", "Software Updates"), ("device manager", "Device Management")):
            if needle in text:
                return label
        return category
