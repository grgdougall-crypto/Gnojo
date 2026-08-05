from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_SCHEMA_VERSION = "1.0"


class CuratorMemoryError(RuntimeError):
    pass


class CuratorMemoryStore:
    """Durable operational memory. This store never writes trusted content."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state_path = self.root / "memory.json"

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            now = datetime.now(timezone.utc).isoformat()
            return {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "created_at": now,
                "updated_at": now,
                "tasks": {},
                "audits": [],
                "decisions": [],
            }
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CuratorMemoryError(f"Unable to read Curator memory: {error}") from error
        if value.get("schema_version") != MEMORY_SCHEMA_VERSION:
            raise CuratorMemoryError(
                f"Unsupported Curator memory schema: {value.get('schema_version')!r}"
            )
        value.setdefault("tasks", {})
        value.setdefault("audits", [])
        value.setdefault("decisions", [])
        return value

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        value = deepcopy(state)
        value["schema_version"] = MEMORY_SCHEMA_VERSION
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = self.root / f".{self.state_path.name}.{os.getpid()}.tmp"
        try:
            temporary.write_text(
                json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.state_path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise CuratorMemoryError(f"Unable to save Curator memory: {error}") from error

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        priority: str | None = None,
        actor: str = "Human",
        note: str = "",
        event_name: str = "updated",
    ) -> dict[str, Any]:
        state = self.load()
        task = state["tasks"].get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        allowed_statuses = {"open", "in_progress", "deferred", "resolved", "ignored", "superseded"}
        allowed_owners = {"Curator", "Researcher", "Workflow Designer", "Script Engineer", "QA Reviewer", "Human"}
        allowed_priorities = {"Critical", "High", "Medium", "Low", "Info"}
        if status and status not in allowed_statuses:
            raise CuratorMemoryError(f"Unsupported task status: {status}")
        if owner and owner not in allowed_owners:
            raise CuratorMemoryError(f"Unsupported task owner: {owner}")
        if priority and priority not in allowed_priorities:
            raise CuratorMemoryError(f"Unsupported task priority: {priority}")
        before = {"status": task["status"], "owner": task["owner"], "priority": task["priority"]}
        if status:
            task["status"] = status
        if owner:
            task["owner"] = owner
        if priority:
            task["priority"] = priority
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event_name,
            "before": before,
            "after": {"status": task["status"], "owner": task["owner"], "priority": task["priority"]},
            "note": note,
        }
        task.setdefault("history", []).append(event)
        task.setdefault("resolution_history", []).append(event)
        state["decisions"].append({"task_id": task_id, **event})
        self.save(state)
        return deepcopy(task)
