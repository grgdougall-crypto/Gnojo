from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_SCHEMA_VERSION = "1.0"


def _growth_defaults() -> dict[str, Any]:
    return {"lessons": {}, "proposals": {}, "evaluations": {}, "event_queue": []}


def _control_defaults() -> dict[str, Any]:
    return {"global_disabled": False, "scheduled_runs_disabled": True}


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
                "growth": _growth_defaults(),
                "controls": _control_defaults(),
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
        growth = value.setdefault("growth", _growth_defaults())
        for key, default in _growth_defaults().items():
            growth.setdefault(key, deepcopy(default))
        controls = value.setdefault("controls", _control_defaults())
        for key, default in _control_defaults().items():
            controls.setdefault(key, default)
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
        metadata: dict[str, Any] | None = None,
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
        current_status = task.get("status", "open")
        allowed_transitions = {
            "open": {"in_progress", "deferred", "ignored", "resolved"},
            "in_progress": {"open", "deferred", "ignored", "resolved"},
            "deferred": {"open", "in_progress", "ignored", "resolved"},
            "ignored": {"open"},
            "resolved": {"open"},
            "superseded": set(),
        }
        if status == current_status:
            # A retried/double-submitted state change is already complete. Do not
            # duplicate decisions, resolution events, or debt attribution.
            return deepcopy(task)
        if status and status not in allowed_transitions.get(current_status, set()):
            raise CuratorMemoryError(
                f"Task cannot move from {current_status.replace('_', ' ')} to {status.replace('_', ' ')}."
            )
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
        if metadata:
            event["metadata"] = deepcopy(metadata)
        if status == "resolved":
            task["resolved_at"] = event["at"]
            task["resolution_notes"] = note
            if metadata:
                task["resolution_metadata"] = deepcopy(metadata)
        elif status == "open" and event_name == "reopen":
            task.pop("resolved_at", None)
            task.pop("resolution_metadata", None)
        task.setdefault("history", []).append(event)
        task.setdefault("resolution_history", []).append(event)
        state["decisions"].append({"task_id": task_id, **event})
        self.save(state)
        return deepcopy(task)

    def record_verification(self, task_id: str, verification: dict[str, Any]) -> dict[str, Any]:
        """Persist a read-only targeted observation without changing task lifecycle."""
        state = self.load()
        task = state["tasks"].get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        value = deepcopy(verification)
        task["current_verification"] = value
        event = {
            "at": value.get("verified_at") or datetime.now(timezone.utc).isoformat(),
            "actor": "Curator",
            "event": "targeted_verification",
            "verification_result": value.get("status"),
            "rule": value.get("rule"),
            "workflow_id": value.get("workflow_id"),
            "node_id": value.get("node_id"),
        }
        # Repeated verification of the same affected fingerprint/result is idempotent.
        history = task.setdefault("history", [])
        previous = next((item for item in reversed(history)
                         if item.get("event") == "targeted_verification"), None)
        if not previous or previous.get("verification_result") != event["verification_result"] or \
                task.get("last_verified_fingerprint") != value.get("affected_fingerprint"):
            history.append(event)
            state.setdefault("decisions", []).append({"task_id": task_id, **event})
        task["last_verified_fingerprint"] = value.get("affected_fingerprint")
        self.save(state)
        return deepcopy(task)
