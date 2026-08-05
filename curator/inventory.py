from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.repositories.command_repository import CommandRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.script_repository import ScriptRepository

from .models import AuditFilter, InventoryRecord


class InventoryError(RuntimeError):
    pass


class CuratorInventory:
    """Build a read-only inventory from Gnojo's existing content stores."""

    def __init__(self, repository_root: Path):
        self.root = repository_root.resolve()

    def collect(self, filters: AuditFilter | None = None) -> list[InventoryRecord]:
        filters = filters or AuditFilter()
        records: list[InventoryRecord] = []
        records.extend(self._workflows())
        records.extend(self._articles())
        records.extend(self._commands())
        records.extend(self._scripts())
        records = [record for record in records if self._matches(record, filters)]
        return sorted(records, key=lambda item: (item.content_type, item.identifier, item.source_path))

    def _workflows(self) -> list[InventoryRecord]:
        records: list[InventoryRecord] = []
        locations = [
            (self.root / "app" / "decision_trees", "built_in"),
            (self.root / "app" / "workflow_drafts", "draft"),
            (self.root / "app" / "workflow_publications", "published"),
        ]
        for directory, state in locations:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    value = self._json(path)
                except InventoryError as error:
                    raw = {"workflow_id": path.stem, "name": path.stem, "nodes": {}, "_inventory_error": str(error)}
                    records.append(self._record("workflow", path.stem, path, raw, state))
                    continue
                workflow = self._unwrap_workflow(value)
                if not workflow:
                    continue
                identifier = str(workflow.get("workflow_id") or path.stem)
                records.append(self._record("workflow", identifier, path, workflow, state))
        return records

    def _articles(self) -> list[InventoryRecord]:
        repository = KnowledgeRepository(self.root / "knowledge_base")
        records: list[InventoryRecord] = []
        for state, articles, directory in (
            ("draft", repository.get_drafts(), repository.draft_directory),
            ("published", repository.get_published(), repository.published_directory),
            ("archived", repository.get_archived(), repository.archive_directory),
        ):
            for article in articles:
                identifier = str(article.get("id") or "unknown")
                records.append(self._record("article", identifier, directory / f"{identifier}.json", article, state))
            loaded = {Path(record.source_path).stem for record in records if record.state == state}
            for path in sorted(directory.glob("*.json")) if directory.exists() else []:
                if path.stem in loaded:
                    continue
                try:
                    self._json(path)
                except InventoryError as error:
                    raw = {"id": path.stem, "title": path.stem, "_inventory_error": str(error)}
                    records.append(self._record("article", path.stem, path, raw, state))
        return records

    def _commands(self) -> list[InventoryRecord]:
        directory = self.root / "knowledge_base" / "commands"
        return [
            self._record("command", str(item.get("id") or "unknown"), directory / f"{item.get('id')}.json", item, str(item.get("review_status") or ""))
            for item in CommandRepository(directory).get_all()
        ]

    def _scripts(self) -> list[InventoryRecord]:
        directory = self.root / "knowledge_base" / "scripts"
        return [
            self._record("script", str(item.get("id") or "unknown"), directory / "catalog.json", item, str(item.get("review_status") or "curated"))
            for item in ScriptRepository(directory).get_all()
        ]

    def _record(self, content_type: str, identifier: str, path: Path, raw: dict[str, Any], state: str) -> InventoryRecord:
        return InventoryRecord(
            content_type=content_type,
            identifier=identifier,
            title=str(raw.get("title") or raw.get("name") or identifier),
            source_path=self._relative(path),
            category=str(raw.get("category") or ""),
            platform=self._platform(raw),
            state=state,
            raw=raw,
        )

    @staticmethod
    def _unwrap_workflow(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        for key in ("workflow", "content", "snapshot"):
            nested = value.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("nodes"), dict):
                return nested
        return value if isinstance(value.get("nodes"), dict) else None

    @staticmethod
    def _platform(raw: dict[str, Any]) -> str:
        value = raw.get("platform") or raw.get("platforms") or ""
        return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)

    def _json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InventoryError(f"Unable to read {self._relative(path)}: {error}") from error

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    @staticmethod
    def _matches(record: InventoryRecord, filters: AuditFilter) -> bool:
        if filters.content_type and record.content_type.casefold() != filters.content_type.casefold():
            return False
        if filters.platform and filters.platform.casefold() not in record.platform.casefold():
            return False
        if filters.category and filters.category.casefold() not in record.category.casefold():
            return False
        return True
