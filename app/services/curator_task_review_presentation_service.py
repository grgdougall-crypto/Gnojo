from __future__ import annotations

from copy import deepcopy
from typing import Any


class CuratorTaskReviewPresentationService:
    """Prepare deterministic task-review summaries without mutating task data."""

    RECENT_HISTORY_LIMIT = 3

    @classmethod
    def present(cls, task: dict[str, Any]) -> dict[str, Any]:
        history = list(reversed(deepcopy(task.get("history") or [])))
        return {
            "history_count": len(history),
            "recent_history": history[: cls.RECENT_HISTORY_LIMIT],
            "remaining_history": history[cls.RECENT_HISTORY_LIMIT :],
            "original_evidence_count": len(task.get("original_evidence") or []),
            "has_current_content": bool(task.get("current_content")),
            "has_verification": bool((task.get("current_verification") or {}).get("status")),
        }
