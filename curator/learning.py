from __future__ import annotations

from collections import Counter
from typing import Any


class CuratorLearningService:
    """Extract evidence-backed patterns; never changes trusted content."""

    def analyze(self, tasks: dict[str, dict[str, Any]]) -> dict[str, Any]:
        active = [task for task in tasks.values() if task.get("status") in {"open", "in_progress"}]
        recurring = [task for task in active if int(task.get("times_observed", 1)) > 1]
        finding_types = Counter(task.get("finding_type", "unknown") for task in recurring)
        categories = Counter(task.get("category") or "Unspecified" for task in active)
        platforms = Counter(task.get("platform") or "Unspecified" for task in active)
        lessons = []
        for finding_type, count in finding_types.most_common():
            lessons.append({
                "pattern": finding_type,
                "observation": f"This finding type has recurred across {count} active task(s).",
                "evidence_task_ids": sorted(task["task_id"] for task in recurring if task.get("finding_type") == finding_type),
                "human_gate": True,
            })
        return {
            "lessons": lessons,
            "recurring_task_count": len(recurring),
            "repeated_finding_types": dict(finding_types.most_common()),
            "active_tasks_by_category": dict(categories.most_common()),
            "active_tasks_by_platform": dict(platforms.most_common()),
            "guardrail": "Patterns inform future review; they do not automatically alter trusted content or policy.",
        }
