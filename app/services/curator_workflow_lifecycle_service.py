from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import ArticleNotFoundError, KnowledgeRepository
from app.services.knowledge_identity_service import KnowledgeIdentityError, KnowledgeIdentityService
from app.services.workflow_publication_service import WorkflowPublicationError, WorkflowPublicationService


@dataclass(frozen=True)
class ActionableWorkflow:
    workflow_id: str
    filename: str
    source_path: str
    lifecycle: str
    workflow: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        value = json.dumps(self.workflow, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CuratorWorkflowLifecycleService:
    """Resolve one workflow through the authoring lifecycle.

    Editorial work uses draft > current published snapshot > built-in. Integrity
    auditing may still inspect every stored lifecycle copy independently.
    """

    PRECEDENCE = ("draft", "published", "built_in")

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()

    def resolve(self, workflow_id: str) -> ActionableWorkflow | None:
        return self._draft(workflow_id) or self._published(workflow_id) or self._built_in(workflow_id)

    def relationship(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        target = self.resolve(workflow_id)
        if not target:
            return {"status": "target_unavailable", "workflow_id": workflow_id, "node_id": node_id}
        node = target.workflow.get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return {"status": "target_unavailable", **self.provenance(target), "node_id": node_id}
        raw = str(node.get("knowledge_article") or "").strip()
        result = {**self.provenance(target), "node_id": node_id, "node": node,
                  "knowledge_article": raw or None, "content_fingerprint": self.fingerprint(node)}
        if not raw:
            return {**result, "status": "relationship_missing"}
        try:
            canonical = KnowledgeIdentityService.canonical_id(raw)
            article = KnowledgeRepository(self.root / "knowledge_base").resolve_published_article(canonical)
            article_id = KnowledgeIdentityService.canonical_id(article)
        except (KnowledgeIdentityError, ArticleNotFoundError):
            return {**result, "status": "relationship_conflict_or_unresolved"}
        return {**result, "status": "relationship_satisfied", "canonical_article_id": article_id,
                "article_title": str(article.get("title") or article_id)}

    @staticmethod
    def provenance(target: ActionableWorkflow) -> dict[str, Any]:
        return {"workflow_id": target.workflow_id, "workflow_filename": target.filename,
                "source_path": target.source_path, "lifecycle": target.lifecycle,
                "workflow_fingerprint": target.fingerprint}

    @staticmethod
    def fingerprint(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _draft(self, workflow_id: str) -> ActionableWorkflow | None:
        directory = self.root / "app" / "workflow_drafts"
        for path in sorted(directory.glob("*.json")) if directory.exists() else ():
            workflow = self._read_workflow(path)
            if workflow and str(workflow.get("workflow_id") or path.stem) == workflow_id:
                return self._target(workflow_id, path.name, path, "draft", workflow)
        return None

    def _published(self, workflow_id: str) -> ActionableWorkflow | None:
        try:
            snapshot = WorkflowPublicationService(self.root / "app" / "workflow_publications").load_current(workflow_id)
        except WorkflowPublicationError:
            return None
        workflow = (snapshot or {}).get("workflow")
        if not isinstance(workflow, dict):
            return None
        publication = snapshot.get("publication", {})
        version = int(publication.get("version") or 0)
        path = self.root / "app" / "workflow_publications" / workflow_id / f"v{version:04d}.json"
        return self._target(workflow_id, str(publication.get("source_filename") or f"{workflow_id}.json"),
                            path, "published", workflow)

    def _built_in(self, workflow_id: str) -> ActionableWorkflow | None:
        directory = self.root / "app" / "decision_trees"
        for path in sorted(directory.glob("*.json")) if directory.exists() else ():
            workflow = self._read_workflow(path)
            if workflow and str(workflow.get("workflow_id") or path.stem) == workflow_id:
                return self._target(workflow_id, path.name, path, "built_in", workflow)
        return None

    def _target(self, workflow_id: str, filename: str, path: Path, lifecycle: str,
                workflow: dict[str, Any]) -> ActionableWorkflow:
        try:
            source = str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            source = str(path.resolve())
        return ActionableWorkflow(workflow_id, filename, source, lifecycle, workflow)

    @staticmethod
    def _read_workflow(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        for key in ("workflow", "content", "snapshot"):
            nested = value.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("nodes"), dict):
                return nested
        return value if isinstance(value.get("nodes"), dict) else None
