from __future__ import annotations

from typing import Any


class CuratorDashboardPresentationService:
    """Build a compact dashboard view without changing task order or state."""

    WORKING_SET_LIMIT = 6

    @classmethod
    def present(cls, tasks: list[dict[str, Any]], *, group_tasks) -> dict[str, Any]:
        ordered = list(tasks)
        working_set = ordered[: cls.WORKING_SET_LIMIT]
        remaining = ordered[cls.WORKING_SET_LIMIT :]
        return {
            "total_count": len(ordered),
            "displayed_count": len(working_set),
            "remaining_count": len(remaining),
            "working_groups": group_tasks(working_set),
            "remaining_groups": group_tasks(remaining),
        }
