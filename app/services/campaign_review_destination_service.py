from __future__ import annotations

from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.curator_workflow_lifecycle_service import (
    CuratorWorkflowLifecycleService,
)
from app.services.workflow_draft_service import WorkflowDraftService


class CampaignReviewDestinationService:
    """Resolve governed review destinations without guessing cross-subsystem URLs."""

    def __init__(self, repository_root: Path):
        self.repository_root = Path(repository_root).resolve()
        self.knowledge = KnowledgeRepository(self.repository_root / "knowledge_base")
        self.identities = ArticleIdentityResolver(self.knowledge)
        self.workflow_path = self.repository_root / "app" / "workflow_drafts"

    def resolve(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        kind = str(opportunity.get("type") or "shared_article")
        if kind == "shared_article":
            identifier = str(opportunity.get("article_id") or "").strip()
            match = self.identities.resolve_published(identifier)
            if match:
                article_id = str(match.article.get("id"))
                return self._found("knowledge", "published_article", article_id,
                                   "view_published", {"article_id": article_id})
            draft = self.identities.resolve(identifier, include_drafts=True)
            if draft and draft.article.get("review_status") != "approved":
                article_id = str(draft.article.get("id"))
                return self._found("knowledge", "article_draft", article_id,
                                   "review_draft", {"article_id": article_id})
            return self._missing(kind, identifier)

        if kind in {"shared_workflow", "workflow"}:
            identifier = str(opportunity.get("workflow_id") or opportunity.get("target_asset") or "").strip()
            drafts = WorkflowDraftService(self.workflow_path).list_drafts() if self.workflow_path.exists() else []
            draft = next((item for item in drafts
                          if not item.get("is_damaged") and item.get("workflow_id") == identifier), None)
            if draft:
                return self._found("workflow_drafts", "workflow_draft", identifier,
                                   "workflow_editor", {"filename": draft["filename"]})
            return self._missing(kind, identifier)
        return self._missing(kind, str(opportunity.get("target_asset") or ""))

    def resolve_learning_authoring(
        self, work_item: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve one learning gap to its exact authoritative workflow."""
        return self.project_learning_authoring(self.repository_root, work_item)

    @classmethod
    def project_learning_authoring(
        cls, repository_root: Path, work_item: dict[str, Any]
    ) -> dict[str, Any]:
        """Project a learning destination without initializing writable stores."""
        workflow_id = str(work_item.get("workflow_id") or "").strip()
        if work_item.get("work_type") != "learning_content" or not workflow_id:
            return cls._missing("learning_content", workflow_id)
        lifecycle = CuratorWorkflowLifecycleService(Path(repository_root).resolve())
        if len(lifecycle.drafts(workflow_id)) > 1:
            return cls._missing("learning_content", workflow_id)
        target = lifecycle.resolve(workflow_id)
        if target is None or target.workflow_id != workflow_id:
            return cls._missing("learning_content", workflow_id)
        return cls._found(
            "workflow",
            "learning_authoring_workflow",
            workflow_id,
            "workflow_studio",
            {"workflow": workflow_id, "_anchor": f"workflow-{workflow_id}"},
        )

    @staticmethod
    def _found(owner, resource_type, resource_id, endpoint, values):
        return {"resolved": True, "owner": owner, "resource_type": resource_type,
                "resource_id": resource_id, "endpoint": endpoint, "route_values": values}

    @staticmethod
    def _missing(kind, identifier):
        return {"resolved": False, "owner": None, "resource_type": kind,
                "resource_id": identifier,
                "reason": f"The governed {kind.replace('_', ' ')} target '{identifier or 'unknown'}' could not be resolved."}
