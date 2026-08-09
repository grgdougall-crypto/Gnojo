from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_tag_service import ArticleTagService
from app.services.knowledge_identity_service import KnowledgeIdentityService
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.workflow_draft_service import WorkflowDraftError, WorkflowDraftService


class KnowledgePublicationError(RuntimeError):
    pass


class KnowledgePublicationService:
    """Publish one reviewed article and its relationship as a recoverable unit."""

    def __init__(self, repository: KnowledgeRepository, workflow_service: WorkflowDraftService | None = None):
        self.repository = repository
        self.workflows = workflow_service or WorkflowDraftService()

    def publish(self, article_id: str, *, reviewer: str = "Gnojo reviewer") -> dict[str, Any]:
        article = KnowledgeIdentityService.normalize(self.repository.get_draft(article_id))
        canonical = article["canonical_id"]
        equivalent = ArticleIdentityResolver(self.repository).resolve(candidate=article)
        if equivalent and equivalent.article.get("id") != canonical:
            raise KnowledgePublicationError(
                f"Equivalent published knowledge already exists as '{equivalent.article['id']}' "
                f"({equivalent.confidence:.1%} confidence). Reuse it or review a guided merge."
            )
        now = datetime.now(timezone.utc).isoformat()
        review = dict(article.get("review") or {})
        review.update({"status": "approved", "reviewed_by": reviewer, "reviewed_at": now})
        article["review"] = review
        article["tags"] = ArticleTagService.normalize(article.get("tags") or ArticleTagService.generate(article))
        history = list(article.get("version_history") or [])
        previous = None
        try:
            previous = self.repository.get_published_article(canonical)
        except Exception:
            pass
        version = int((previous or article).get("version") or 0) + (1 if previous else 0)
        if version < 1:
            version = 1
        article.update({"version": version, "published_at": now})
        history.append({"version": version, "published_at": now, "reviewed_by": reviewer})
        article["version_history"] = history

        origin = article.get("workflow_origin") if isinstance(article.get("workflow_origin"), dict) else None
        workflow_before = None
        workflow_path = None
        if origin and origin.get("filename") and origin.get("node_id"):
            workflow_path = self.workflows.drafts_path / Path(str(origin["filename"])).name
            workflow_before = workflow_path.read_bytes() if workflow_path.exists() else None

        published_path = self.repository.published_directory / f"{canonical}.json"
        published_before = published_path.read_bytes() if published_path.exists() else None
        draft_path = self.repository.draft_directory / f"{canonical}.json"
        inventory_path = self.repository.knowledge_base_directory / "inventory.json"
        inventory_before = inventory_path.read_bytes() if inventory_path.exists() else None
        try:
            self.repository.save_draft(article, overwrite=True)
            if origin:
                self.workflows.update_node(
                    str(origin["filename"]), str(origin["node_id"]),
                    {"knowledge_article": canonical},
                )
            self.repository.publish_article(canonical, overwrite=True)
            # Refresh only the generated inventory. The full Curator audit remains
            # an explicit operation because it can be comparatively expensive.
            from app.services.knowledge_integrity_service import KnowledgeIntegrityService
            KnowledgeIntegrityService(self.repository.knowledge_base_directory.parent).rebuild_index()
        except Exception as error:
            self._restore(published_path, published_before)
            self._restore(workflow_path, workflow_before)
            self._restore(inventory_path, inventory_before)
            if not draft_path.exists():
                self.repository.save_draft(article, overwrite=True)
            raise KnowledgePublicationError(
                f"Publication was rolled back because one required update failed: {error}"
            ) from error
        return article

    @staticmethod
    def _restore(path: Path | None, content: bytes | None) -> None:
        if not path:
            return
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
