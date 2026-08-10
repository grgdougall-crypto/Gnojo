from __future__ import annotations

from collections import Counter
import hashlib
from copy import deepcopy
from typing import Any

from curator.calibration import ReasoningCalibrationService


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
        calibration_service = ReasoningCalibrationService()
        lessons.extend(calibration_service.recurring_lessons(tasks.values()))
        return {
            "lessons": lessons,
            "recurring_task_count": len(recurring),
            "repeated_finding_types": dict(finding_types.most_common()),
            "active_tasks_by_category": dict(categories.most_common()),
            "active_tasks_by_platform": dict(platforms.most_common()),
            "guardrail": "Patterns inform future review; they do not automatically alter trusted content or policy.",
            "reasoning_calibration": calibration_service.summary(tasks.values()),
        }

    def persist_proposed_lessons(self, state: dict[str, Any], analysis: dict[str, Any],
                                 *, observed_at: str) -> list[str]:
        """Persist evidence-backed patterns as human-gated operational lessons."""
        lessons = state.setdefault("growth", {}).setdefault("lessons", {})
        recorded: list[str] = []
        for item in analysis.get("lessons", []):
            identity = str(item.get("pattern") or "").strip().casefold()
            if not identity:
                continue
            lesson_id = "CGL-" + hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
            current = lessons.get(lesson_id)
            evidence = sorted(set(item.get("evidence_task_ids") or []))
            if current:
                current["supporting_evidence"] = sorted(set(current.get("supporting_evidence", []) + evidence))
                current["observations"] = max(int(current.get("observations", 1)), len(evidence))
                current["updated_at"] = observed_at
            else:
                lessons[lesson_id] = {
                    "lesson_id": lesson_id, "pattern_observed": item["pattern"],
                    "supporting_evidence": evidence, "observations": max(1, len(evidence)),
                    "confidence": "medium", "affected_domains": [], "human_decisions": [],
                    "recommended_future_behavior": (
                        "Review this recurring pattern and decide whether a capability, rule, or template should be proposed."
                    ),
                    "status": "proposed", "created_at": observed_at, "updated_at": observed_at,
                    "decision_history": [], "human_gate": True,
                }
            recorded.append(lesson_id)
        return recorded
