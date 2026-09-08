from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.services.knowledge_campaign_orchestration_service import (
    ACTION_POLICY,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_coverage_planner_service import (
    COMMAND_HANDOFF_IDENTITY_FIELDS,
    KnowledgeCoveragePlannerService,
)
from app.services.review_workspace_service import ReviewWorkspaceService


AUTONOMOUS_ACTOR = "Autonomous Growth Stage 2"
SUPPORTED_GAP_TYPES = {
    "missing_article", "weak_learning_coverage",
    "missing_command_reference", "missing_workflow",
}
GAP_TYPE_PRIORITY = {
    "missing_article": 0,
    "weak_learning_coverage": 1,
    "missing_command_reference": 2,
    "missing_workflow": 3,
}


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
        # The real orchestration graph is intentionally lazy so --preview does
        # not create any of its runtime package directories as a side effect.
        self.orchestration = orchestration
        self.max_transitions = max(1, int(max_transitions))
        self.max_external_operations = max(0, int(max_external_operations))

    def run(self, *, preview: bool = False) -> AutonomousGrowthResult:
        try:
            assessments = [
                self.planner.assess_domain(domain["id"])
                for domain in self.planner.domains()
            ]
            stage2_candidates = (
                self.planner.assess_stage2_candidates()
                if hasattr(self.planner, "assess_stage2_candidates") else []
            )
            candidate = self._select(assessments, stage2_candidates)
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
            prior_fingerprint = (
                ((equivalent or {}).get("creation_metadata") or {}).get(
                    "assessment_fingerprint"
                )
            )
            if equivalent and prior_fingerprint and candidate.get("assessment_fingerprint") and (
                prior_fingerprint != candidate["assessment_fingerprint"]
            ):
                return self._result(
                    "BLOCKED", preview, candidate, candidate["selection_explanation"],
                    campaign=self._campaign_summary(equivalent, "stale_equivalent"),
                    preparation={
                        "outcome": "blocked",
                        "reason": "The equivalent campaign was created from a different authoritative assessment.",
                    },
                    validation={"status": "blocked", "reason": "Campaign evidence is stale."},
                )

            reconciliation, reconciliation_error = self._command_handoff_reconciliation(
                candidate, equivalent
            )
            if reconciliation_error:
                return self._result(
                    "BLOCKED", preview, candidate, candidate["selection_explanation"],
                    campaign=self._campaign_summary(equivalent, "reconciliation_unavailable"),
                    preparation={
                        "outcome": "blocked",
                        "reason": reconciliation_error,
                    },
                    validation={
                        "status": "blocked",
                        "reason": "Legacy command relationship handoff could not be reconciled safely.",
                    },
                )

            if preview:
                disposition = (
                    "would_reconcile" if reconciliation
                    else "reuse" if equivalent
                    else "create"
                )
                return self._result(
                    "SELECTED",
                    True,
                    candidate,
                    candidate["selection_explanation"],
                    campaign=(
                        self._campaign_summary(equivalent, disposition)
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
                        "planned_artifact": candidate["intended_artifact"],
                        "intended_preparation_stages": candidate["intended_preparation_stages"],
                        "expected_human_gate": candidate["expected_human_gate"],
                        "reconciliation": deepcopy(reconciliation),
                    },
                    validation={"status": "not_run", "reason": "Preview performs no writes."},
                )

            if reconciliation:
                campaign = self.planner.reconcile_command_reference_handoff(
                    equivalent["campaign_id"],
                    reconciliation["work_item_id"],
                    reconciliation["binding"],
                    relationship_handoff=reconciliation.get("relationship_handoff"),
                    expected_fingerprint=reconciliation["campaign_fingerprint"],
                    actor=AUTONOMOUS_ACTOR,
                )
                disposition = "reconciled"
            else:
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

    def _select(self, assessments: list[dict[str, Any]],
                stage2_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        candidates = []
        for assessment in assessments:
            areas = {
                item["area_id"]: item
                for item in assessment.get("areas", [])
            }
            domain = assessment["domain"]
            for gap in assessment.get("gaps", []):
                area = areas.get(gap.get("area_id"), {})
                gap_type = gap.get("gap_type")
                if gap_type == "missing_article":
                    eligible = self._eligible_missing_article(gap, area)
                    identity = f"{domain['id']}:{gap['area_id']}:missing_article"
                    intended_artifact = "knowledge_article"
                    expected_gate = "Source research approval"
                    stages = ["source_research", "evidence", "claims", "article_draft"]
                elif gap_type == "missing_workflow":
                    eligible = self._eligible_missing_workflow(gap, area)
                    identity = f"domain:{domain['id']}:topic:{gap['area_id']}:missing_workflow"
                    intended_artifact = "workflow_draft"
                    expected_gate = "Source or workflow claim review"
                    stages = ["source_research", "evidence", "workflow_claims", "workflow_draft"]
                else:
                    continue
                if not eligible:
                    continue
                deficiency = 100 - int(area.get("coverage_percent", 100))
                candidate = {
                    "gap_identity": identity,
                    "gap_type": gap_type,
                    "title": gap["summary"],
                    "domain_id": domain["id"],
                    "domain_title": domain["title"],
                    "area_id": gap["area_id"],
                    "area_title": gap["area_title"],
                    "confidence": gap.get("confidence"),
                    "evidence": list(gap.get("evidence") or []),
                    "evidence_strength": (
                        len(gap.get("evidence") or []) if gap_type == "missing_article"
                        else int(area.get("article_count", 0)) + int(area.get("command_count", 0))
                    ),
                    "runtime_relevance": 0,
                    "measurable_deficiency": deficiency,
                    "intended_artifact": intended_artifact,
                    "intended_preparation_stages": stages,
                    "expected_human_gate": expected_gate,
                    "assessment_fingerprint": assessment["fingerprint"],
                    "coverage_percent": area.get("coverage_percent"),
                    "workflow_count": area.get("workflow_count", 0),
                    "relevant_node_count": area.get("relevant_node_count", 0),
                    "article_count": area.get("article_count", 0),
                    "command_count": area.get("command_count", 0),
                    "ranking": {
                        "type_priority": GAP_TYPE_PRIORITY[gap_type],
                        "evidence_strength": (
                            len(gap.get("evidence") or []) if gap_type == "missing_article"
                            else int(area.get("article_count", 0)) + int(area.get("command_count", 0))
                        ),
                        "coverage_deficiency": deficiency,
                        "runtime_relevance": 0,
                        "workflow_context": int(area.get("workflow_count", 0)),
                        "relevant_nodes": int(area.get("relevant_node_count", 0)),
                        "stable_tiebreaker": identity,
                    },
                }
                candidate["selection_explanation"] = self._selection_explanation(candidate)
                candidates.append(candidate)
        for raw in stage2_candidates or []:
            candidate = self._normalize_stage2_candidate(raw)
            if candidate:
                candidate["selection_explanation"] = self._selection_explanation(candidate)
                candidates.append(candidate)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                item["ranking"]["type_priority"],
                -item["ranking"]["evidence_strength"],
                -item["ranking"]["coverage_deficiency"],
                -item["ranking"]["runtime_relevance"],
                -item["ranking"]["workflow_context"],
                -item["ranking"]["relevant_nodes"],
                item["gap_identity"],
            ),
        )

    @staticmethod
    def _eligible_missing_article(gap: dict[str, Any], area: dict[str, Any]) -> bool:
        return bool(
            gap.get("gap_type") == "missing_article"
            and gap.get("confidence") == "high"
            and gap.get("evidence")
            and area.get("workflow_count", 0) > 0
            and area.get("relevant_node_count", 0) > 0
        )

    @staticmethod
    def _eligible_missing_workflow(gap: dict[str, Any], area: dict[str, Any]) -> bool:
        return bool(
            gap.get("gap_type") == "missing_workflow"
            and gap.get("confidence") == "high"
            and gap.get("evidence")
            and area.get("workflow_count", 0) == 0
            and area.get("article_count", 0) >= 1
            and area.get("command_count", 0) >= 1
            and len(area.get("asset_ids") or []) >= 2
            and area.get("safety_ambiguous_command_count", 0) == 0
        )

    @staticmethod
    def _normalize_stage2_candidate(raw):
        gap_type = raw.get("gap_type")
        if gap_type not in {"weak_learning_coverage", "missing_command_reference"}:
            return None
        if raw.get("confidence") != "high" or not all(
            raw.get(key) for key in ("gap_identity", "title", "domain_id", "workflow_id", "evidence")
        ):
            return None
        if gap_type == "weak_learning_coverage" and not raw.get("node_ids"):
            return None
        if gap_type == "missing_command_reference" and not all(
            raw.get(key) for key in ("node_id", "command_identity", "command_risk")
        ):
            return None
        candidate = deepcopy(raw)
        candidate.setdefault("area_id", candidate["workflow_id"])
        candidate.setdefault("area_title", candidate["workflow_id"].replace("_", " ").title())
        candidate.setdefault("coverage_percent", None)
        candidate.setdefault("workflow_count", 1)
        candidate.setdefault("relevant_node_count", len(candidate.get("node_ids") or [candidate.get("node_id")]))
        candidate.setdefault("measurable_deficiency", 1)
        candidate.setdefault("evidence_strength", len(candidate["evidence"]))
        candidate.setdefault("runtime_relevance", 0)
        candidate.setdefault("assessment_fingerprint", "")
        candidate.setdefault(
            "intended_preparation_stages",
            ["learning_content_plan"] if gap_type == "weak_learning_coverage"
            else ["command_relationship_review"],
        )
        candidate.setdefault(
            "expected_human_gate",
            "Workflow Designer learning authoring" if gap_type == "weak_learning_coverage"
            else "Command Library relationship review",
        )
        candidate.setdefault(
            "intended_artifact",
            "learning_content_plan" if gap_type == "weak_learning_coverage"
            else "command_relationship_review",
        )
        candidate["ranking"] = {
            "type_priority": GAP_TYPE_PRIORITY[gap_type],
            "evidence_strength": int(candidate["evidence_strength"]),
            "coverage_deficiency": int(candidate["measurable_deficiency"]),
            "runtime_relevance": int(candidate["runtime_relevance"]),
            "workflow_context": 1,
            "relevant_nodes": int(candidate["relevant_node_count"]),
            "stable_tiebreaker": candidate["gap_identity"],
        }
        return candidate

    @staticmethod
    def _selection_explanation(candidate):
        gap_type = candidate["gap_type"]
        if gap_type == "missing_article":
            basis = (
                f"an authoritative workflow covers the area ({candidate['workflow_count']} workflow, "
                f"{candidate['relevant_node_count']} relevant nodes) but supporting article coverage is missing"
            )
        elif gap_type == "weak_learning_coverage":
            basis = (
                f"learning coverage is {candidate['coverage_percent']}%, below the explicit 50% "
                "Content Quality threshold, with stable workflow and node identities"
            )
        elif gap_type == "missing_command_reference":
            basis = (
                f"structured article command data resolves '{candidate['command_identity']}' to an "
                "existing risk-classified Command Library record whose explicit relationship is incomplete"
            )
        else:
            basis = (
                f"no workflow exists while {candidate['article_count']} article and "
                f"{candidate['command_count']} command assets provide converging topic evidence"
            )
        higher = ", ".join(
            name.replace("_", " ") for name, priority in GAP_TYPE_PRIORITY.items()
            if priority < GAP_TYPE_PRIORITY[gap_type]
        ) or "none"
        return (
            f"Selected {candidate['area_title']} ({gap_type}) because {basis}. It ranked after "
            f"checking higher-priority supported types ({higher}); deterministic identity is "
            f"{candidate['gap_identity']}."
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
            notes="Created by one bounded Autonomous Growth Stage 1/2 run.",
            actor=AUTONOMOUS_ACTOR,
            metadata={
                "initiated_by": (
                    "autonomous_growth_stage1" if candidate["gap_type"] == "missing_article"
                    else "autonomous_growth_stage2"
                ),
                "gap_identity": candidate["gap_identity"],
                "selection_basis": candidate["selection_explanation"],
                "assessment_fingerprint": candidate["assessment_fingerprint"],
                "selected_gap": deepcopy(candidate),
            },
        )
        return self.planner.analyze(campaign["campaign_id"]), "created"

    def _command_handoff_reconciliation(
        self,
        candidate: dict[str, Any],
        equivalent: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, str]:
        if candidate.get("gap_type") != "missing_command_reference" or not equivalent:
            return None, ""
        binding = {
            key: str(candidate.get(key) or "").strip()
            for key in COMMAND_HANDOFF_IDENTITY_FIELDS
        }
        risk = candidate.get("command_risk")
        if not all(binding.values()) or not isinstance(risk, dict) or not risk.get("level"):
            return None, "Current authoritative command relationship identity is incomplete."

        metadata = equivalent.get("creation_metadata")
        selected = metadata.get("selected_gap") if isinstance(metadata, dict) else None
        if (
            not isinstance(selected, dict)
            or metadata.get("initiated_by") != "autonomous_growth_stage2"
            or selected.get("gap_type") != "missing_command_reference"
        ):
            return None, "The equivalent campaign lacks authoritative Stage 2 command-gap provenance."
        gaps = [gap for gap in equivalent.get("gaps") or [] if (
            isinstance(gap, dict)
            and gap.get("gap_type") == "missing_command_reference"
            and gap.get("area_id") == candidate.get("area_id")
        )]
        if len(gaps) != 1:
            return None, "The equivalent campaign command gap is ambiguous."
        gap = gaps[0]
        work_items = [item for item in equivalent.get("work_items") or [] if (
            isinstance(item, dict)
            and item.get("gap_id") == gap.get("gap_id")
            and item.get("work_type") == "command_reference"
        )]
        if len(work_items) != 1:
            return None, "The equivalent campaign command work item is ambiguous."
        work = work_items[0]

        for record in (selected, gap, work):
            for key, expected in binding.items():
                current = record.get(key)
                if current not in (None, "") and str(current) != expected:
                    return None, f"The equivalent campaign {key} conflicts with current evidence."
        metadata_identity = metadata.get("gap_identity")
        if metadata_identity not in (None, "") and str(metadata_identity) != binding["gap_identity"]:
            return None, "The equivalent campaign gap identity conflicts with current evidence."

        missing = [
            f"{record_name}.{key}"
            for record_name, record in (("selected_gap", selected), ("gap", gap), ("work_item", work))
            for key in COMMAND_HANDOFF_IDENTITY_FIELDS
            if record.get(key) in (None, "")
        ]
        if metadata.get("gap_identity") in (None, ""):
            missing.append("creation_metadata.gap_identity")

        try:
            records = KnowledgeCampaignOrchestrationService.read_persisted(self.campaign_root)
        except Exception:
            return None, "The existing campaign orchestration could not be read safely."
        orchestrations = [
            record for record in records
            if record.get("campaign_id") == equivalent.get("campaign_id")
        ]
        if len(orchestrations) != 1:
            return None, "The existing campaign orchestration is missing or ambiguous."
        states = [state for state in orchestrations[0].get("work_item_states") or [] if (
            isinstance(state, dict) and state.get("work_item_id") == work.get("work_item_id")
        )]
        if len(states) != 1 or not (
            states[0].get("state") == "awaiting_human_review"
            and states[0].get("action_authority") == "human_gate"
            and states[0].get("next_action") == "review_command_reference"
        ):
            return None, "The existing work item is no longer at the command-reference human gate."

        projection = ReviewWorkspaceService(
            self.repository_root
        ).command_relationship_review_status(
            equivalent["campaign_id"], work["work_item_id"]
        )
        if projection.get("projectable"):
            if missing:
                return None, "Projectable command review has inconsistent campaign identity metadata."
            return None, ""
        projection_reason = str(projection.get("reason") or "")
        relationship_handoff = None
        if projection_reason == "missing_relationship_declaration_handoff":
            relationship_handoff = projection.get("missing_prerequisite")
            if not isinstance(relationship_handoff, dict):
                return None, "The command relationship handoff prerequisite is unavailable."
        elif projection_reason != "work_identity_incomplete" or not missing:
            return None, (
                "The strict command relationship Review projection rejected current state: "
                f"{projection_reason or 'unknown conflict'}."
            )

        return {
            "campaign_id": equivalent["campaign_id"],
            "orchestration_id": orchestrations[0].get("orchestration_id"),
            "work_item_id": work["work_item_id"],
            "binding": binding,
            "missing_fields": missing,
            "projectable_before": False,
            "missing_prerequisite": projection_reason,
            "canonical_review_key": projection.get("canonical_review_key"),
            "relationship_handoff": deepcopy(relationship_handoff),
            "campaign_fingerprint": self.planner.campaign_fingerprint(equivalent),
        }, ""

    @staticmethod
    def _work_item(campaign: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
        gap_ids = {
            gap["gap_id"]
            for gap in campaign.get("gaps") or []
            if gap.get("area_id") == candidate["area_id"]
            and gap.get("gap_type") == candidate["gap_type"]
        }
        allowed_work_type = {
            "missing_article": "knowledge_article",
            "weak_learning_coverage": "learning_content",
            "missing_command_reference": "command_reference",
            "missing_workflow": "workflow",
        }[candidate["gap_type"]]
        matches = [
            item
            for item in campaign.get("work_items") or []
            if item.get("gap_id") in gap_ids and item.get("work_type") == allowed_work_type
        ]
        return matches[0] if len(matches) == 1 else None

    def _prepare(
        self,
        candidate: dict[str, Any],
        campaign: dict[str, Any],
        work_item: dict[str, Any],
        disposition: str,
    ) -> AutonomousGrowthResult:
        orchestration = self._orchestration()
        record = orchestration.get_or_create(
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
            record = orchestration.advance_item(
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

    def _orchestration(self):
        if self.orchestration is None:
            self.orchestration = KnowledgeCampaignOrchestrationService(
                self.repository_root,
                self.campaign_root,
                planner=self.planner,
                max_transitions=self.max_transitions,
                max_external_operations=self.max_external_operations,
            )
        return self.orchestration

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
        review_workspace_link = "/review"
        if state.get("next_action") == "review_command_reference" and state.get("work_item_id"):
            review_workspace_link += "?" + urlencode({
                "item": f"command_relationship_review:{state['work_item_id']}"
            })
        return {
            "required": state.get("action_authority") == "human_gate",
            "action": state.get("next_action"),
            "specialized_review_link": state.get("review_link"),
            "campaign_control_link": (
                f"/curator/growth/coverage-campaigns/{record['campaign_id']}/orchestration"
            ),
            "review_workspace_link": review_workspace_link,
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
