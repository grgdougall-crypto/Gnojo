from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CLASSIFICATION_WEIGHT = {"Defect": 8.0, "Risk": 5.0, "Opportunity": 3.0, "Recommendation": 2.0}
PRIORITY_WEIGHT = {"Critical": 5.0, "High": 4.0, "Medium": 3.0, "Low": 2.0, "Info": 1.0}


class KnowledgeDebtService:
    """Calculate explainable debt from active persistent tasks."""

    def calculate(self, tasks: dict[str, dict[str, Any]], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        active = [task for task in tasks.values() if task.get("status") in {"open", "in_progress"}]
        for task in active:
            age_days = max(0, (now - datetime.fromisoformat(task["first_seen"])).days)
            recurrence = min(4.0, max(0, int(task.get("times_observed", 1)) - 1) * 0.5)
            age = min(3.0, age_days / 30.0)
            score = (
                CLASSIFICATION_WEIGHT.get(task.get("classification"), 2.0)
                + PRIORITY_WEIGHT.get(task.get("priority"), 1.0)
                + recurrence + age
            )
            task["knowledge_debt_score"] = round(score, 2)

        total = round(sum(task["knowledge_debt_score"] for task in active), 2)
        previous_total = float((previous or {}).get("total", 0))
        by = {}
        for field in ("classification", "content_type", "category", "platform"):
            values: dict[str, float] = {}
            for task in active:
                key = task.get(field) or "Unspecified"
                values[key] = round(values.get(key, 0) + task["knowledge_debt_score"], 2)
            by[field] = dict(sorted(values.items()))
        ranked = sorted(active, key=lambda item: (-item["knowledge_debt_score"], item["task_id"]))
        has_baseline = previous is not None
        return {
            "total": total,
            "previous_total": previous_total,
            "change": None if not has_baseline else round(total - previous_total, 2),
            "trend": "baseline" if not has_baseline else "increasing" if total > previous_total else "decreasing" if total < previous_total else "stable",
            "active_task_count": len(active),
            "breakdown": by,
            "largest_contributors": [self._summary(task) for task in ranked[:10]],
            "oldest_items": [self._summary(task) for task in sorted(active, key=lambda item: item["first_seen"])[:10]],
            "fastest_growing": [self._summary(task) for task in sorted(active, key=lambda item: (-int(item.get("times_observed", 1)), item["task_id"]))[:10]],
            "blocked_content": sorted({value for task in active if task.get("classification") == "Defect" for value in task.get("related_content", [])}),
        }

    @staticmethod
    def _summary(task: dict[str, Any]) -> dict[str, Any]:
        return {key: task.get(key) for key in ("task_id", "title", "classification", "priority", "status", "knowledge_debt_score", "first_seen", "times_observed")}
