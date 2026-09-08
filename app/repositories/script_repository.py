import json
from pathlib import Path

from app.data_root import resolve_data_path


class ScriptRepository:
    """Loads curated diagnostic script metadata and source text."""

    def __init__(self, base_path=None):
        self.base_path = resolve_data_path(
            "knowledge_base", "scripts",
            explicit_path=base_path,
            legacy_path="knowledge_base/scripts",
        )
        self.catalog_path = self.base_path / "catalog.json"

    def get_all(self):
        if not self.catalog_path.exists():
            return []
        records = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []

    def get(self, script_id):
        for record in self.get_all():
            if record.get("id") == script_id:
                result = dict(record)
                result["source"] = self.source_path(result).read_text(encoding="utf-8")
                return result
        return None

    def source_path(self, record):
        filename = record.get("filename", "")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Script filename is invalid.")
        path = (self.base_path / filename).resolve()
        if path.parent != self.base_path.resolve() or path.suffix.lower() not in {".ps1", ".sh", ".zsh", ".bat"}:
            raise ValueError("Script path is invalid.")
        return path
