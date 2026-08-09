from __future__ import annotations

from pathlib import Path
from typing import Any

from curator.memory import CuratorMemoryStore


class CuratorTaskReconciliationService:
    """Resolve only tasks whose recorded evidence matches a verified repair."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")

    def reconcile(self, item: dict[str, Any], *, session_id: str, verified: bool) -> list[str]:
        if not verified:
            return []
        evidence = item.get("affected_content", {})
        needles = {str(value).lower() for value in (
            evidence.get("workflow"), evidence.get("node"), evidence.get("current_reference"), evidence.get("before")
        ) if value}
        state = self.store.load()
        resolved: list[str] = []
        for task_id, task in state.get("tasks", {}).items():
            if task.get("status") in {"resolved", "ignored", "superseded"}:
                continue
            haystack = " ".join(str(value) for value in (
                task.get("content_identifier"), task.get("title"), task.get("description"),
                task.get("recommended_action"), task.get("evidence"), task.get("related_content")
            )).lower()
            if needles and sum(needle in haystack for needle in needles) >= min(2, len(needles)):
                self.store.update_task(task_id, status="resolved", actor="Curator Fix Wizard",
                                       note=f"Resolved by verified integrity repair ({session_id}).",
                                       event_name="verified_integrity_repair")
                resolved.append(task_id)
        return resolved

    def reconcile_external(self, item: dict[str, Any], *, session_id: str, reason: str) -> list[str]:
        """Resolve matching tasks only after targeted integrity evidence removed the finding."""
        return self._update_matching(item, session_id=session_id, status="resolved",
                                     event="resolved_by_external_verified_change", note=reason)

    def reconcile_classification(self, item: dict[str, Any], *, session_id: str,
                                 previous: str | None, current: str | None) -> list[str]:
        return self._update_matching(
            item, session_id=session_id, status=None, event="repair_classification_changed",
            note=f"Fix Wizard classification changed from {previous or 'unknown'} to {current or 'unknown'}.",
        )

    def _update_matching(self, item: dict[str, Any], *, session_id: str, status: str | None,
                         event: str, note: str) -> list[str]:
        evidence = item.get("affected_content", {})
        explicit = evidence.get("task_id")
        needles = {str(value).lower() for value in (
            evidence.get("workflow"), evidence.get("node"), evidence.get("current_reference"),
            evidence.get("before"), evidence.get("id")
        ) if value}
        state = self.store.load()
        updated = []
        for task_id, task in state.get("tasks", {}).items():
            haystack = " ".join(str(value) for value in (
                task.get("content_identifier"), task.get("title"), task.get("description"),
                task.get("recommended_action"), task.get("evidence"), task.get("related_content")
            )).lower()
            matches = task_id == explicit or (needles and sum(value in haystack for value in needles) >= min(2, len(needles)))
            if not matches:
                continue
            self.store.update_task(task_id, status=status, actor="Curator Fix Wizard",
                                   note=f"{note} ({session_id})", event_name=event)
            updated.append(task_id)
        return updated
