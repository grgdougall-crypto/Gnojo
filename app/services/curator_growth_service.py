from __future__ import annotations

from pathlib import Path
from typing import Any

from curator.growth import CuratorGrowthService as GrowthStoreService
from curator.memory import CuratorMemoryStore


class CuratorGrowthService:
    """Application-facing Curator Growth review service."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.growth = GrowthStoreService(self.store)

    def dashboard(self) -> dict[str, Any]:
        return self.growth.dashboard()

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

