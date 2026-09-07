from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.services.knowledge_campaign_orchestration_service import (
    ACTION_POLICY,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerService,
)


AUTONOMOUS_ACTOR = "Autonomous Growth Stage 1"
SUPPORTED_GAP_TYPES = {"missing_article"}


@dataclass(frozen=True)
class AutonomousGrowthResult:
    status: str
    preview: bool
    selected_gap: dict[str, Any] | None
    selection_explanation: str
    campaign: dict[str, Any] | None
    preparation: dict[str, Any]
    validation: dict[str, Any]
    human_review: dict[str, Any]
    intentional_non_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutonomousGrowthService:
    """One bounded, deterministic draft-preparation pass over coverage gaps."""

    def __init__(
        self,
        repository_root: Path | None = None,
        campaign_root: Path | None = None,
        *,
        planner: KnowledgeCoveragePlannerService | None = None,
        orchestration: KnowledgeCampaignOrchestrationService | None = None,
        max_transitions: int = 12,
        max_external_operations: int = 1,
    ):
        self.repository_root = (
            repository_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.campaign_root = (
            campaign_root or self.repository_root / "knowledge_campaigns"
        ).resolve()
        self.planner = planner or KnowledgeCoveragePlannerService(
            self.repository_root, self.campaign_root
        )
        self.orchestration = orchestration or KnowledgeCampaignOrchestrationService(
            self.repository_root,
            self.campaign_root,
            planner=self.planner,
            max_transitions=max_transitions,
            max_external_operations=max_external_operations,
        )
        self.max_transitions = max(1, int(max_transitions))
        self.max_external_operations = max(0, int(max_external_operations))

    def run(self, *, preview: bool = False) -> AutonomousGrowthResult:
        try:
            assessments = [
                self.planner.assess_domain(domain["id"])
                for domain in self.planner.domains()
            ]
            candidate = self._select(assessments)
            if candidate is None:
                return self._result(
                    "NO-OP",
                    preview,
                    None,
                    "No supported, evidence-backed knowledge coverage gap is available.",
                    preparation={"outcome": "not_started"},
                    validation={"status": "not_run", "reason": "No gap was selected."},
                )

            equivalents = self._equivalent_campaigns(candidate)
            if len(equivalents) > 1:
                return self._result(
                    "BLOCKED",
                    preview,
                    candidate,
                    candidate["selection_explanation"],
                    preparation={
                        "outcome": "blocked",
                        "reason": "Equivalent campaign identity is ambiguous.",
                        "campaign_ids": [item["campaign_id"] for item in equivalents],
                    },
                    validation={"status": "blocked", "reason": "Campaign identity is ambiguous."},
                )

            equivalent = equivalents[0] if equivalents else None
            if equivalent and equivalent.get("status") in {"completed", "archived"}:
                return self._result(
                    "NO-OP",
                    preview,
                    candidate,
                    candidate["selection_explanation"],
                    campaign=self._campaign_summary(equivalent, "completed_equivalent"),
                    preparation={
                        "outcome": "not_started",
                        "reason": "An equivalent campaign has already completed.",
                    },
                    validation={"status": "not_run", "reason": "No new work is required."},
                )

            if preview:
                disposition = "reuse" if equivalent else "create"
                return self._result(
                    "SELECTED",
                    True,
                    candidate,
                    candidate["selection_explanation"],
                    campaign=(
                        self._campaign_summary(equivalent, "would_reuse")
                        if equivalent
                        else {
                            "campaign_id": None,
                            "disposition": "would_create",
                            "gap_identity": candidate["gap_identity"],
                        }
                    ),
                    preparation={
                        "outcome": "preview",
                        "campaign_disposition": disposition,
                        "planned_artifact": "knowledge_article",
                    },
                    validation={"status": "not_run", "reason": "Preview performs no writes."},
                )

            campaign, disposition = self._create_or_reuse(candidate, equivalent)
            work_item = self._work_item(campaign, candidate)
            if work_item is None:
                return self._result(
                    "BLOCKED",
                    False,
                    candidate,
                    candidate["selection_explanation"],
                    campaign=self._campaign_summary(campaign, disposition),
                    preparation={
                        "outcome": "blocked",
                        "reason": "The selected gap is not represented by exactly one campaign work item.",
                    },
                    validation={"status": "blocked", "reason": "Selected work identity is unavailable."},
                )
            return self._prepare(candidate, campaign, work_item, disposition)
        except Exception as error:
            return self._result(
                "BLOCKED",
                preview,
                None,
                "Authoritative Growth state could not be resolved safely.",
                preparation={
                    "outcome": "blocked",
                    "reason": f"{type(error).__name__}: {error}",
                },
                validation={
                    "status": "blocked",
                    "reason": "Authoritative state or an existing pipeline service failed.",
                },
            )

    def _select(self, assessments: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = []
        for assessment in assessments:
            areas = {
                item["area_id"]: item
                for item in assessment.get("areas", [])
            }
            domain = assessment["domain"]
            for gap in assessment.get("gaps", []):
                area = areas.get(gap.get("area_id"), {})
                if not self._eligible_gap(gap, area):
                    continue
                identity = f"{domain['id']}:{gap['area_id']}:{gap['gap_type']}"
                deficiency = 100 - int(area.get("coverage_percent", 100))
                candidate = {
                    "gap_identity": identity,
                    "gap_type": gap["gap_type"],
                    "title": gap["summary"],
                    "domain_id": domain["id"],
                    "domain_title": domain["title"],
                    "area_id": gap["area_id"],
                    "area_title": gap["area_title"],
                    "confidence": gap.get("confidence"),
                    "evidence": list(gap.get("evidence") or []),
                    "assessment_fingerprint": assessment["fingerprint"],
                    "coverage_percent": area.get("coverage_percent"),
                    "workflow_count": area.get("workflow_count", 0),
                    "relevant_node_count": area.get("relevant_node_count", 0),
                    "ranking": {
                        "supported_gap": 1,
                        "coverage_deficiency": deficiency,
                        "workflow_context": int(area.get("workflow_count", 0)),
                        "relevant_nodes": int(area.get("relevant_node_count", 0)),
                        "stable_tiebreaker": identity,
                    },
                }
                candidate["selection_explanation"] = (
                    f"Selected {gap['area_title']} because an authoritative workflow already "
                    f"covers the area ({candidate['workflow_count']} workflow, "
                    f"{candidate['relevant_node_count']} relevant nodes) while supporting article "
                    f"coverage is missing; measured coverage is {candidate['coverage_percent']}%."
                )
                candidates.append(candidate)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                -item["ranking"]["supported_gap"],
                -item["ranking"]["coverage_deficiency"],
                -item["ranking"]["workflow_context"],
                -item["ranking"]["relevant_nodes"],
                item["gap_identity"],
            ),
        )

    @staticmethod
    def _eligible_gap(gap: dict[str, Any], area: dict[str, Any]) -> bool:
        return bool(
            gap.get("gap_type") in SUPPORTED_GAP_TYPES
            and gap.get("confidence") == "high"
            and gap.get("evidence")
            and area.get("workflow_count", 0) > 0
            and area.get("relevant_node_count", 0) > 0
        )

    def _equivalent_campaigns(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        matches = []
        for campaign in self.planner.list_campaigns():
            metadata = campaign.get("creation_metadata") or {}
            exact = metadata.get("gap_identity") == candidate["gap_identity"]
            represented = campaign.get("domain") == candidate["domain_id"] and any(
                gap.get("area_id") == candidate["area_id"]
                and gap.get("gap_type") == candidate["gap_type"]
                for gap in campaign.get("gaps") or []
            )
            if exact or represented:
                matches.append(campaign)
        return sorted(matches, key=lambda item: item["campaign_id"])

    def _create_or_reuse(
        self, candidate: dict[str, Any], equivalent: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str]:
        if equivalent:
            campaign = equivalent
            disposition = "reused"
            if not campaign.get("last_analyzed_at"):
                campaign = self.planner.analyze(campaign["campaign_id"])
            return campaign, disposition
        campaign = self.planner.create(
            title=f"{candidate['area_title']} Knowledge Coverage",
            domain_id=candidate["domain_id"],
            objective=f"Prepare governed supporting knowledge for {candidate['area_title']}.",
            notes="Created by one bounded Autonomous Growth Stage 1 run.",
            actor=AUTONOMOUS_ACTOR,
            metadata={
                "initiated_by": "autonomous_growth_stage1",
                "gap_identity": candidate["gap_identity"],
                "selection_basis": candidate["selection_explanation"],
                "assessment_fingerprint": candidate["assessment_fingerprint"],
            },
        )
        return self.planner.analyze(campaign["campaign_id"]), "created"

    @staticmethod
    def _work_item(campaign: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
        gap_ids = {
            gap["gap_id"]
            for gap in campaign.get("gaps") or []
            if gap.get("area_id") == candidate["area_id"]
            and gap.get("gap_type") == candidate["gap_type"]
        }
        matches = [
            item
            for item in campaign.get("work_items") or []
            if item.get("gap_id") in gap_ids and item.get("work_type") == "knowledge_article"
        ]
        return matches[0] if len(matches) == 1 else None

    def _prepare(
        self,
        candidate: dict[str, Any],
        campaign: dict[str, Any],
        work_item: dict[str, Any],
        disposition: str,
    ) -> AutonomousGrowthResult:
        record = self.orchestration.get_or_create(
            campaign["campaign_id"], mode="supervised", actor=AUTONOMOUS_ACTOR
        )
        outcomes: list[dict[str, Any]] = []
        external_operations = 0
        for _ in range(self.max_transitions):
            state = self._state(record, work_item["work_item_id"])
            if state is None:
                return self._blocked_preparation(candidate, campaign, disposition, outcomes,
                                                 "Selected work item disappeared during preparation.")
            if state.get("action_authority") == "human_gate":
                return self._ready_for_review(candidate, campaign, disposition, record, state, outcomes)
            if state.get("state") == "complete":
                return self._result(
                    "NO-OP", False, candidate, candidate["selection_explanation"],
                    campaign=self._campaign_summary(campaign, disposition),
                    preparation={"outcome": "already_prepared", "artifacts": outcomes},
                    validation={"status": "passed", "basis": "existing_pipeline_projection"},
                    human_review=self._human_review(record, state),
                )
            if state.get("action_authority") != "machine_safe":
                reason = (state.get("blocker") or {}).get("explanation") or (
                    "No safe autonomous preparation action is available."
                )
                return self._blocked_preparation(candidate, campaign, disposition, outcomes, reason)
            action = state.get("next_action")
            policy = ACTION_POLICY.get(action)
            if not policy or policy.get("authority") != "machine_safe":
                return self._blocked_preparation(
                    candidate, campaign, disposition, outcomes,
                    "The next pipeline action is not explicitly allowlisted as machine-safe.",
                )
            if policy.get("external"):
                if external_operations >= self.max_external_operations:
                    return self._blocked_preparation(
                        candidate, campaign, disposition, outcomes,
                        "The bounded external-operation limit was reached.",
                    )
                external_operations += 1
            record = self.orchestration.advance_item(
                record["orchestration_id"], work_item["work_item_id"],
                actor=AUTONOMOUS_ACTOR,
            )
            outcome = (record.get("execution") or {}).get("outcomes", [{}])[-1]
            outcomes.append(deepcopy(outcome))
            if outcome.get("status") not in {"completed", "package_reused"}:
                return self._blocked_preparation(
                    candidate, campaign, disposition, outcomes,
                    outcome.get("message") or "The existing campaign pipeline failed.",
                )
        return self._blocked_preparation(
            candidate, campaign, disposition, outcomes,
            "The bounded preparation transition limit was reached.",
        )

    @staticmethod
    def _state(record: dict[str, Any], work_item_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in record.get("work_item_states", [])
             if item.get("work_item_id") == work_item_id),
            None,
        )

    def _ready_for_review(self, candidate, campaign, disposition, record, state, outcomes):
        return self._result(
            "SELECTED", False, candidate, candidate["selection_explanation"],
            campaign={
                **self._campaign_summary(campaign, disposition),
                "orchestration_id": record["orchestration_id"],
            },
            preparation={
                "outcome": "prepared_for_human_review",
                "stage": state.get("stage"),
                "artifacts": outcomes,
            },
            validation={
                "status": "passed",
                "basis": "existing_pipeline_projection",
                "reason": "The existing pipeline reached its next governed human gate.",
            },
            human_review=self._human_review(record, state),
        )

    def _blocked_preparation(self, candidate, campaign, disposition, outcomes, reason):
        return self._result(
            "BLOCKED", False, candidate, candidate["selection_explanation"],
            campaign=self._campaign_summary(campaign, disposition),
            preparation={"outcome": "blocked", "reason": reason, "artifacts": outcomes},
            validation={"status": "blocked", "reason": reason},
        )

    @staticmethod
    def _human_review(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required": state.get("action_authority") == "human_gate",
            "action": state.get("next_action"),
            "specialized_review_link": state.get("review_link"),
            "campaign_control_link": (
                f"/curator/growth/coverage-campaigns/{record['campaign_id']}/orchestration"
            ),
            "review_workspace_link": "/review",
            "orientation": (
                "Use the specialized review link for this artifact; existing Review items "
                "remain available in the Review workspace."
            ),
        }

    @staticmethod
    def _campaign_summary(campaign: dict[str, Any], disposition: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign.get("campaign_id"),
            "status": campaign.get("status"),
            "disposition": disposition,
            "gap_identity": (campaign.get("creation_metadata") or {}).get("gap_identity"),
        }

    @staticmethod
    def _result(
        status: str,
        preview: bool,
        selected_gap: dict[str, Any] | None,
        explanation: str,
        *,
        campaign: dict[str, Any] | None = None,
        preparation: dict[str, Any],
        validation: dict[str, Any],
        human_review: dict[str, Any] | None = None,
    ) -> AutonomousGrowthResult:
        return AutonomousGrowthResult(
            status=status,
            preview=preview,
            selected_gap=deepcopy(selected_gap),
            selection_explanation=explanation,
            campaign=deepcopy(campaign),
            preparation=deepcopy(preparation),
            validation=deepcopy(validation),
            human_review=deepcopy(human_review or {"required": False}),
            intentional_non_actions=(
                "No content or workflow was published.",
                "No Growth lesson or proposal was approved.",
                "No Curator task was resolved.",
                "No repair was executed.",
                "No second gap or campaign was selected in this run.",
            ),
        )
