from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.data_root import resolve_data_root
from app.services.autonomous_growth_service import (
    SUPPORTED_GAP_TYPES,
    AutonomousGrowthService,
)


POLICY_ID = "supervised-library-growth-batch"
POLICY_VERSION = 1
MAX_BATCH_SIZE = 3


@dataclass(frozen=True)
class SupervisedLibraryGrowthBatchResult:
    status: str
    preview: bool
    policy_id: str
    policy_version: int
    domain: dict[str, str]
    requested_limit: int
    selected_count: int
    items: tuple[dict[str, Any], ...]
    reason: str
    intentional_non_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SupervisedLibraryGrowthBatchService:
    """Select and prepare one deterministic, supervised Growth batch."""

    def __init__(
        self,
        repository_root: Path | None = None,
        *,
        growth: AutonomousGrowthService | None = None,
    ):
        self.repository_root = resolve_data_root(
            repository_root, legacy_root=Path(__file__).resolve().parents[2]
        )
        self.growth = growth or AutonomousGrowthService(self.repository_root)

    def run(
        self, *, domain: str, limit: int = MAX_BATCH_SIZE, preview: bool = False
    ) -> SupervisedLibraryGrowthBatchResult:
        requested_limit = int(limit)
        if requested_limit < 1 or requested_limit > MAX_BATCH_SIZE:
            return self._result(
                "BLOCKED", preview, {}, requested_limit, (),
                reason=f"Batch limit must be between 1 and {MAX_BATCH_SIZE}.",
            )
        try:
            resolved_domain = self._resolve_domain(domain)
            candidates = self.growth.ranked_candidates(resolved_domain["id"])
            candidates = self._unique_candidates(candidates)
        except Exception as error:
            return self._result(
                "BLOCKED", preview, {}, requested_limit, (),
                reason=f"Authoritative Growth candidates could not be resolved safely: {error}",
            )

        selected = candidates[:requested_limit]
        if not selected:
            return self._result(
                "NO-OP", preview, resolved_domain, requested_limit, (),
                reason="No supported, evidence-backed growth candidate is available in this domain.",
            )

        items = []
        for position, candidate in enumerate(selected, start=1):
            outcome = self.growth.prepare_ranked_candidate(candidate, preview=preview)
            items.append({
                "position": position,
                "gap_type": candidate["gap_type"],
                "gap_identity": candidate["gap_identity"],
                "priority": deepcopy(candidate["ranking"]),
                "rationale": candidate["selection_explanation"],
                "intended_artifact": candidate["intended_artifact"],
                "existing_work_disposition": (
                    (outcome.campaign or {}).get("disposition") or "unavailable"
                ),
                "outcome": outcome.as_dict(),
            })

        blocked = [item for item in items if item["outcome"]["status"] == "BLOCKED"]
        status = "BLOCKED" if len(blocked) == len(items) else (
            "PARTIAL" if blocked else "SELECTED"
        )
        return self._result(
            status, preview, resolved_domain, requested_limit, tuple(items)
        )

    def _resolve_domain(self, requested: str) -> dict[str, str]:
        normalized = str(requested or "").strip().casefold()
        matches = [
            item for item in self.growth.planner.domains()
            if normalized in {
                str(item.get("id") or "").strip().casefold(),
                str(item.get("title") or "").strip().casefold(),
            }
        ]
        if len(matches) != 1:
            raise ValueError(
                "Requested domain must match exactly one configured coverage domain."
            )
        return {"id": matches[0]["id"], "title": matches[0]["title"]}

    @staticmethod
    def _unique_candidates(
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        identities: set[str] = set()
        unique = []
        for candidate in candidates:
            identity = str(candidate.get("gap_identity") or "")
            if candidate.get("gap_type") not in SUPPORTED_GAP_TYPES:
                raise ValueError("Growth candidate type is not allowlisted.")
            if not identity or identity in identities:
                raise ValueError("Growth candidate identity is missing or ambiguous.")
            identities.add(identity)
            unique.append(deepcopy(candidate))
        return unique

    @staticmethod
    def _result(
        status: str,
        preview: bool,
        domain: dict[str, str],
        requested_limit: int,
        items: tuple[dict[str, Any], ...],
        *,
        reason: str = "",
    ) -> SupervisedLibraryGrowthBatchResult:
        return SupervisedLibraryGrowthBatchResult(
            status=status,
            preview=preview,
            policy_id=POLICY_ID,
            policy_version=POLICY_VERSION,
            domain=deepcopy(domain),
            requested_limit=requested_limit,
            selected_count=len(items),
            items=tuple(deepcopy(items)),
            reason=reason,
            intentional_non_actions=(
                "No content or workflow was published.",
                "No human-review decision was made.",
                "No command was executed.",
                "No recursive batch was selected.",
            ),
        )
