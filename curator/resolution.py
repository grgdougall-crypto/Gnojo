from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RESOLUTION_SCHEMA_VERSION = "1.0"


class ResolutionPackageError(RuntimeError):
    pass


class ResolutionPackageRepository:
    """Persistent, versioned Curator proposals. This store never edits trusted content."""

    def __init__(self, root: Path):
        self.root = root.resolve() / "resolution_packages"

    def get(self, task_id: str) -> dict[str, Any] | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResolutionPackageError(f"Unable to read resolution package: {error}") from error
        return value if isinstance(value, dict) else None

    def save(self, package: dict[str, Any]) -> dict[str, Any]:
        task_id = package.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ResolutionPackageError("A task ID is required.")
        current = self.get(task_id)
        value = deepcopy(package)
        now = datetime.now(timezone.utc).isoformat()
        value["schema_version"] = RESOLUTION_SCHEMA_VERSION
        value["version"] = int((current or {}).get("version", 0)) + 1
        value["created_at"] = (current or {}).get("created_at", now)
        value["updated_at"] = now
        history = list((current or {}).get("history", []))
        history.append({
            "at": now,
            "version": value["version"],
            "event": "prepared" if current is None else "refreshed",
            "recommendation": value.get("recommendation"),
        })
        value["history"] = history
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(task_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ResolutionPackageError(f"Unable to save resolution package: {error}") from error
        return deepcopy(value)

    def record_event(self, task_id: str, event: str, **details: Any) -> dict[str, Any]:
        value = self.get(task_id)
        if not value:
            raise ResolutionPackageError("Resolution package was not found.")
        value.setdefault("history", []).append({
            "at": datetime.now(timezone.utc).isoformat(), "event": event, **details
        })
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(task_id)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return value

    def list_all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        values = []
        for path in sorted(self.root.glob("*.json")):
            value = self.get(path.stem)
            if value:
                values.append(value)
        return values

    def _path(self, task_id: str) -> Path:
        safe = str(task_id).strip()
        if not safe or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in safe):
            raise ResolutionPackageError("Invalid task ID.")
        return self.root / f"{safe}.json"
