import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_identity_service import KnowledgeIdentityError, KnowledgeIdentityService
from app.services.article_identity_resolver import ArticleIdentityResolver
from curator.inventory import CuratorInventory


class KnowledgeIntegrityError(RuntimeError):
    pass


class KnowledgeIntegrityService:
    """Explain and safely repair cross-store knowledge identity problems."""

    def __init__(self, root: Path | None = None):
        self.root = (root or Path(__file__).resolve().parents[2]).resolve()
        self.repository = KnowledgeRepository(self.root / "knowledge_base")
        self.identities = ArticleIdentityResolver(self.repository)

    def report(self) -> dict[str, Any]:
        inventory = CuratorInventory(self.root).collect()
        articles = [item for item in inventory if item.content_type == "article"]
        workflows = [item for item in inventory if item.content_type == "workflow"]
        by_id = defaultdict(list)
        by_title = defaultdict(list)
        for item in articles:
            by_id[item.identifier].append(item)
            by_title[KnowledgeIdentityService.normalized_title(item.title)].append(item)
        published_ids = {item.identifier for item in articles if item.state == "published"}
        references = []
        broken = []
        inbound = Counter()
        for workflow in workflows:
            for node_id, node in (workflow.raw.get("nodes") or {}).items():
                if not isinstance(node, dict) or not node.get("knowledge_article"):
                    continue
                target = str(node["knowledge_article"]).strip()
                relation = {"workflow": workflow.identifier, "node": node_id, "article": target, "source": workflow.source_path}
                references.append(relation)
                inbound[target] += 1
                if target not in published_ids:
                    broken.append(relation)
        duplicate_groups = []
        seen = set()
        for identity, items in by_id.items():
            live = [item for item in items if item.state == "published"]
            if len(live) > 1:
                duplicate_groups.append(self._group("canonical_id", identity, live))
                seen.update(item.source_path for item in live)
        for title, items in by_title.items():
            live = [item for item in items if item.state == "published"]
            if len(live) > 1 and not all(item.source_path in seen for item in live):
                duplicate_groups.append(self._group("title", title, live))
        published = [item for item in articles if item.state == "published"]
        # Detect high-confidence equivalents whose IDs and titles are not byte-identical.
        grouped_ids = {record["id"] for group in duplicate_groups for record in group["records"]}
        for index, left in enumerate(published):
            for right in published[index + 1:]:
                if left.identifier in grouped_ids and right.identifier in grouped_ids:
                    continue
                confidence, reasoning = self.identities.similarity(left.raw, right.raw)
                if confidence >= self.identities.THRESHOLD:
                    group = self._group("semantic_similarity", left.identifier, [left, right])
                    group["confidence"] = round(confidence * 100, 1)
                    group["reasoning"] = reasoning
                    duplicate_groups.append(group)
                    grouped_ids.update((left.identifier, right.identifier))
        missing_review = [
            {"id": item.identifier, "title": item.title, "source": item.source_path}
            for item in published
            if not (item.raw.get("review") or {}).get("reviewed_by")
            or not (item.raw.get("review") or {}).get("reviewed_at")
        ]
        orphans = [
            {"id": item.identifier, "title": item.title, "source": item.source_path}
            for item in published if not inbound[item.identifier]
        ]
        inventory_path = self.root / "knowledge_base" / "inventory.json"
        indexed_ids: set[str] = set()
        if inventory_path.exists():
            try:
                indexed_ids = {
                    str(item.get("id")) for item in
                    (json.loads(inventory_path.read_text(encoding="utf-8")).get("articles") or [])
                    if isinstance(item, dict) and item.get("id")
                }
            except (OSError, json.JSONDecodeError, AttributeError):
                indexed_ids = set()
        inventory_mismatch = sorted(published_ids.symmetric_difference(indexed_ids))
        counts = {
            "published_articles": len(published), "draft_articles": sum(item.state == "draft" for item in articles),
            "broken_relationships": len(broken), "duplicate_groups": len(duplicate_groups),
            "missing_review_metadata": len(missing_review), "orphaned_articles": len(orphans),
            "inventory_mismatches": len(inventory_mismatch),
            "duplicate_articles": sum(max(0, len(group["records"]) - 1) for group in duplicate_groups),
            "merge_candidates": sum(len(group["records"]) for group in duplicate_groups),
            "archived_articles": self.repository.count_archived(),
            "knowledge_debt_reduction_potential": sum(max(0, len(group["records"]) - 1) for group in duplicate_groups),
            "estimated_cleanup_minutes": sum(max(0, len(group["records"]) - 1) * 8 for group in duplicate_groups),
        }
        return {"counts": counts, "broken_relationships": broken, "duplicate_groups": duplicate_groups,
                "missing_review_metadata": missing_review, "orphaned_articles": orphans,
                "inventory_mismatches": inventory_mismatch,
                "references": references, "explanations": {
                    "broken_relationships": "Workflow links whose canonical article is not currently published.",
                    "duplicate_groups": "Multiple live records that appear to represent one logical article.",
                    "missing_review_metadata": "Published records without a reviewer identity or approval time.",
                    "orphaned_articles": "Published articles with no inbound workflow link; they may still be valid search content.",
                "inventory_mismatches": "Canonical published IDs that disagree with the generated knowledge inventory.",
                }}

    def merge_preview(self, canonical_id: str, duplicate_ids: list[str]) -> dict[str, Any]:
        canonical = self.repository.get_published_article(canonical_id)
        duplicates = [self.repository.get_published_article(value) for value in duplicate_ids if value != canonical_id]
        if not duplicates:
            raise KnowledgeIntegrityError("Choose at least one duplicate to merge.")
        additions: dict[str, list[Any]] = {}
        for field in ("checklist", "commands", "sources", "tags", "related_topics", "related_articles", "version_history"):
            existing = list(canonical.get(field) or [])
            proposed: list[Any] = []
            for article in duplicates:
                for value in article.get(field) or []:
                    if value not in existing and value not in proposed:
                        proposed.append(value)
            additions[field] = proposed
        duplicate_set = {item["id"] for item in duplicates}
        updates = [ref for ref in self.report()["references"] if ref["article"] in duplicate_set]
        return {"canonical": canonical, "duplicates": duplicates, "additions": additions,
                "workflow_updates": updates,
                "aliases": {item["id"]: canonical_id for item in duplicates},
                "history_event": {"event": "merged", "merged_ids": sorted(duplicate_set)}}

    def lifecycle_policy(self, article_id: str) -> dict[str, Any]:
        state = None
        article = None
        for name, getter in (("published", self.repository.get_published_article), ("archived", self.repository.get_archived_article), ("deleted", self.repository.get_deleted_article)):
            try:
                article = getter(article_id); state = name; break
            except Exception:
                continue
        if not article:
            raise KnowledgeIntegrityError(f"Article '{article_id}' was not found.")
        aliases = self.identities.aliases()
        references = [item for item in self.report()["references"] if item["article"] == article_id]
        draft_exists = any(item.get("id") == article_id for item in self.repository.get_drafts())
        alias_dependencies = [key for key, value in aliases.items() if value == article_id]
        merge_history = [item for item in article.get("version_history", []) if isinstance(item, dict) and item.get("event") == "merged"]
        canonical = bool(alias_dependencies) or article_id not in aliases
        archive_reasons = [] if state == "published" else ["Only published articles can be archived."]
        if references:
            archive_reasons.append(f"Referenced by {len(references)} workflow node(s); update those references or merge first.")
        soft_reasons = []
        if state != "archived": soft_reasons.append("The article must be archived first.")
        if references: soft_reasons.append(f"Referenced by {len(references)} workflow node(s).")
        if canonical: soft_reasons.append("This record is a canonical article.")
        if alias_dependencies: soft_reasons.append(f"Used by {len(alias_dependencies)} alias(es).")
        permanent_reasons = []
        if state != "deleted": permanent_reasons.append("The article must be soft deleted first.")
        if references: permanent_reasons.append(f"Referenced by {len(references)} workflow node(s).")
        if canonical: permanent_reasons.append("This record is a canonical article.")
        if alias_dependencies: permanent_reasons.append(f"Used by {len(alias_dependencies)} alias(es).")
        if draft_exists: permanent_reasons.append("A draft with this identity exists.")
        if merge_history: permanent_reasons.append("Merge history must be preserved.")
        return {"article": article, "state": state, "references": references, "aliases": alias_dependencies,
                "can_archive": not archive_reasons, "archive_reasons": archive_reasons,
                "can_soft_delete": not soft_reasons, "soft_delete_reasons": soft_reasons,
                "can_permanent_delete": not permanent_reasons, "permanent_delete_reasons": permanent_reasons}

    def rebuild_index(self) -> Path:
        report = self.report()
        payload = {"schema_version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
                   "articles": [{"id": item.get("canonical_id") or item["id"], "title": item.get("title"),
                                 "category": item.get("category"), "version": item.get("version", 1)}
                                for item in self.repository.get_published()],
                   "integrity": report["counts"]}
        path = self.root / "knowledge_base" / "inventory.json"
        self.repository._write_json_atomic(path, payload)
        return path

    def normalize_identities(self) -> dict[str, Any]:
        """Add canonical identity only where the existing valid ID proves it."""
        changed: list[str] = []
        for directory in (self.repository.draft_directory, self.repository.published_directory, self.repository.archive_directory):
            for path in sorted(directory.glob("*.json")):
                try:
                    article = json.loads(path.read_text(encoding="utf-8"))
                    normalized = KnowledgeIdentityService.normalize(article)
                except (OSError, json.JSONDecodeError, KnowledgeIdentityError):
                    continue
                if normalized != article:
                    self.repository._write_json_atomic(path, normalized)
                    changed.append(str(path.relative_to(self.root)).replace("\\", "/"))
        self.rebuild_index()
        return {"changed": changed, "count": len(changed)}

    def merge(self, canonical_id: str, duplicate_ids: list[str]) -> dict[str, Any]:
        canonical = deepcopy(self.repository.get_published_article(canonical_id))
        canonical = KnowledgeIdentityService.normalize(canonical)
        duplicates = []
        for duplicate_id in duplicate_ids:
            if duplicate_id == canonical_id:
                continue
            duplicates.append(self.repository.get_published_article(duplicate_id))
        if not duplicates:
            raise KnowledgeIntegrityError("Choose at least one duplicate to merge.")
        for article in duplicates:
            for field in ("checklist", "commands", "tags", "sources", "related_topics", "related_articles", "version_history"):
                values = list(canonical.get(field) or [])
                for value in article.get(field) or []:
                    if value not in values:
                        values.append(value)
                canonical[field] = values
        history = list(canonical.get("version_history") or [])
        history.append({"event": "merged", "at": datetime.now(timezone.utc).isoformat(),
                        "merged_ids": [item.get("id") for item in duplicates]})
        canonical["version_history"] = history
        duplicate_ids = [item for item in dict.fromkeys(duplicate_ids) if item != canonical_id]
        workflow_paths = (
            list((self.root / "app" / "workflow_drafts").glob("*.json"))
            + list((self.root / "app" / "decision_trees").glob("*.json"))
            + list((self.root / "app" / "workflow_publications").glob("*.json"))
        )
        snapshots: dict[Path, bytes | None] = {
            path: path.read_bytes() if path.exists() else None for path in workflow_paths
        }
        for article_id in [canonical_id, *duplicate_ids]:
            for directory in (self.repository.published_directory, self.repository.archive_directory):
                path = directory / f"{article_id}.json"
                snapshots[path] = path.read_bytes() if path.exists() else None
        for path in (self.identities.alias_path, self.root / "knowledge_base" / "inventory.json"):
            snapshots[path] = path.read_bytes() if path.exists() else None
        try:
            self.repository.save_published(canonical, overwrite=True)
            for workflow_path in workflow_paths:
                self._replace_workflow_references(workflow_path, set(duplicate_ids), canonical_id)
            for article in duplicates:
                archived_copy = deepcopy(article)
                archived_history = list(archived_copy.get("version_history") or [])
                archived_history.append({
                    "event": "merged_into_canonical",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "canonical_id": canonical_id,
                })
                archived_copy["version_history"] = archived_history
                archived_copy["lifecycle"] = {
                    **(archived_copy.get("lifecycle") or {}),
                    "state": "published",
                    "merged_into": canonical_id,
                }
                self.repository.save_published(archived_copy, overwrite=True)
                self.repository.archive_article(article["id"], overwrite=True)
            aliases = self.identities.aliases()
            aliases.update({article["id"]: canonical_id for article in duplicates})
            self.identities.save_aliases(aliases)
            self.rebuild_index()
        except Exception as error:
            for path, content in snapshots.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            raise KnowledgeIntegrityError(f"Merge was rolled back because a required update failed: {error}") from error
        return canonical

    def archive(self, article_id: str) -> Path:
        policy = self.lifecycle_policy(article_id)
        if not policy["can_archive"]:
            raise KnowledgeIntegrityError(" ".join(policy["archive_reasons"]))
        article = deepcopy(self.repository.get_published_article(article_id))
        history = list(article.get("version_history") or [])
        history.append({"event": "archived", "at": datetime.now(timezone.utc).isoformat()})
        article["version_history"] = history
        article["lifecycle"] = {**(article.get("lifecycle") or {}), "state": "published"}
        self.repository.save_published(article, overwrite=True)
        try:
            return self.repository.archive_article(article_id)
        except Exception as error:
            self.repository.save_published(article, overwrite=True)
            raise KnowledgeIntegrityError(f"Archive failed without removing the published record: {error}") from error

    def soft_delete(self, article_id: str) -> Path:
        policy = self.lifecycle_policy(article_id)
        if not policy["can_soft_delete"]:
            raise KnowledgeIntegrityError(" ".join(policy["soft_delete_reasons"]))
        return self.repository.soft_delete_article(article_id)

    def permanent_delete(self, article_id: str, confirmation: str) -> None:
        policy = self.lifecycle_policy(article_id)
        if confirmation != "This permanently removes the article and cannot be undone.":
            raise KnowledgeIntegrityError("Exact permanent-deletion confirmation is required.")
        if not policy["can_permanent_delete"]:
            raise KnowledgeIntegrityError(" ".join(policy["permanent_delete_reasons"]))
        self.repository.permanently_delete_article(article_id)

    def _replace_workflow_references(self, path: Path, duplicate_ids: set[str], canonical_id: str) -> None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        workflow = document
        if isinstance(document, dict):
            for key in ("workflow", "content", "snapshot"):
                if isinstance(document.get(key), dict) and isinstance(document[key].get("nodes"), dict):
                    workflow = document[key]
                    break
        changed = False
        nodes = (workflow.get("nodes") or {}).values() if isinstance(workflow, dict) else []
        for node in nodes:
            if isinstance(node, dict) and node.get("knowledge_article") in duplicate_ids:
                node["knowledge_article"] = canonical_id
                changed = True
        if changed:
            self.repository._write_json_atomic(path, document)

    @staticmethod
    def _group(reason: str, key: str, items: list[Any]) -> dict[str, Any]:
        return {"reason": reason, "key": key, "confidence": 100.0,
                "identity_reasoning": "Records share an exact canonical or normalized-title identity.", "records": [
            {"id": item.identifier, "title": item.title, "state": item.state, "source": item.source_path,
             "review": item.raw.get("review") or {}, "sources": item.raw.get("sources") or [],
             "tags": item.raw.get("tags") or []} for item in items
        ]}
