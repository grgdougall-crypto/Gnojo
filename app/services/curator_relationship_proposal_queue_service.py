from __future__ import annotations

from pathlib import Path
from typing import Any

from curator.memory import CuratorMemoryStore

from app.services.curator_task_service import CuratorTaskService


class CuratorRelationshipProposalQueueService:
    """Read-only supervisory projection of actionable relationship proposals."""

    FINDING_TYPE = "article_command_reciprocity_conflict"
    ACTIONABLE_STATUSES = ("open", "in_progress", "deferred")
    OUTCOMES = ("add_reciprocal", "remove_unsupported", "human_review_required")

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.tasks = CuratorTaskService(self.root)

    def queue(self, *, outcome: str = "", status: str = "") -> dict[str, Any]:
        selected_outcome = outcome if outcome in self.OUTCOMES else ""
        selected_status = status if status in self.ACTIONABLE_STATUSES else ""
        state = self.store.load()
        relationship_tasks = [
            task for task in state.get("tasks", {}).values()
            if task.get("finding_type") == self.FINDING_TYPE
        ]
        actionable = [
            task for task in relationship_tasks
            if str(task.get("status") or "").casefold() in self.ACTIONABLE_STATUSES
        ]
        items = []
        for task in sorted(actionable, key=self._sort_key):
            projected = self.tasks.get(str(task.get("task_id") or ""))
            proposal = projected.get("relationship_repair_proposal")
            if not proposal:
                continue
            item = {
                "task_id": projected.get("task_id"),
                "task_status": projected.get("status"),
                **proposal,
            }
            if selected_outcome and item["outcome"] != selected_outcome:
                continue
            if selected_status and item["task_status"] != selected_status:
                continue
            items.append(item)
        return {
            "items": items,
            "actionable_count": len(actionable),
            "visible_count": len(items),
            "closed_count": sum(
                str(task.get("status") or "").casefold() in {"resolved", "ignored", "superseded"}
                for task in relationship_tasks
            ),
            "filters": {"outcome": selected_outcome, "status": selected_status},
            "options": {"outcomes": self.OUTCOMES, "statuses": self.ACTIONABLE_STATUSES},
        }

    @staticmethod
    def _sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
        priorities = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}
        return (
            priorities.get(str(task.get("priority") or ""), 5),
            str(task.get("first_seen") or ""),
            str(task.get("task_id") or ""),
        )
