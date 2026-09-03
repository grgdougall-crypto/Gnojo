from __future__ import annotations

from pathlib import Path
from typing import Any

from curator.growth import CuratorGrowthService as GrowthStoreService
from curator.memory import CuratorMemoryStore
from curator.workflow_reasoning import WorkflowReasoningAuditor


class CuratorGrowthService:
    """Application-facing Curator Growth review service."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.growth = GrowthStoreService(self.store)

    def dashboard(self) -> dict[str, Any]:
        dashboard = self.growth.dashboard()
        dashboard["lessons"] = [self._present_lesson(lesson) for lesson in dashboard["lessons"]]
        return dashboard

    @staticmethod
    def _present_lesson(lesson: dict[str, Any]) -> dict[str, Any]:
        """Add reviewer labels without changing the stored Growth lesson identity."""
        value = dict(lesson)
        raw_identity = str(lesson.get("pattern_observed") or "").strip()
        parts = raw_identity.split(":")
        if len(parts) == 4 and parts[0].casefold() == "reasoning_calibration":
            rule_id = parts[1].upper()
            rule_label = WorkflowReasoningAuditor.RULE_LABELS.get(rule_id)
            value.update({
                "display_category": "Reasoning Calibration",
                "display_title": rule_label or CuratorGrowthService._humanize(raw_identity),
                "rule_id": rule_id,
                "calibration_id": parts[2].upper(),
                "raw_identity": raw_identity,
            })
        else:
            value["display_title"] = CuratorGrowthService._humanize(raw_identity)
        return value

    @staticmethod
    def _humanize(value: str) -> str:
        label = value.replace("_", " ").replace("-", " ").strip()
        return label.title() if label else "Unspecified pattern"

    def decide(self, subject_type: str, subject_id: str, status: str, *, reviewer: str,
               reason: str) -> dict[str, Any]:
        if subject_type == "proposal":
            return self.growth.decide_proposal(subject_id, status, reviewer=reviewer, reason=reason)
        if subject_type == "lesson":
            return self.growth.decide_lesson(subject_id, status, reviewer=reviewer, reason=reason)
        raise ValueError("Unsupported Curator Growth subject.")

    def set_control(self, control: str, disabled: bool, *, reviewer: str,
                    reason: str) -> dict[str, Any]:
        return self.growth.set_control(control, disabled, reviewer=reviewer, reason=reason)

