from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CampaignBlockerDestinationService:
    """Read-only resolver for exact governed workspaces behind campaign blockers."""

    def __init__(self, campaign_root: Path | None = None):
        self.campaign_root = Path(campaign_root or Path.cwd() / "knowledge_campaigns").resolve()

    def resolve(self, campaign: dict[str, Any], work_item: dict[str, Any],
                blocker: dict[str, Any] | None) -> dict[str, Any]:
        blocker = blocker or {}
        if blocker.get("blocker_type") != "workflow_eligibility":
            return self._missing("This blocker has no governed internal destination.")
        campaign_id = str(campaign.get("campaign_id") or "")
        work_item_id = str(work_item.get("work_item_id") or "")
        if not campaign_id or not work_item_id:
            return self._missing("Campaign or work-item identity is missing.")

        plans = self._matching("claim_planning", "KCPM-*.json", campaign_id, work_item_id)
        if len(plans) == 1:
            return self._found("Review Workflow Claims", "knowledge_claim_planning_detail",
                               {"plan_id": plans[0]["claim_plan_id"]}, "claim_planning")
        if len(plans) > 1:
            return self._missing("Multiple current claim plans match this work item; reconcile them before continuing.")

        extractions = self._matching("evidence_extraction", "KEX-*.json", campaign_id, work_item_id)
        if len(extractions) == 1:
            extraction = extractions[0]
            approved_units = [unit for unit in extraction.get("evidence_units") or []
                              if unit.get("review_state") == "approved"]
            if extraction.get("status") in {"approved", "partially_approved"} and approved_units:
                return self._found("Plan Workflow Claims", "knowledge_workflow_claim_planning_prepare",
                                   {"campaign_id": campaign_id, "work_item_id": work_item_id},
                                   "workflow_claim_planning")
            return self._found("Review Evidence", "knowledge_evidence_extraction_detail",
                               {"extraction_id": extractions[0]["extraction_id"]},
                               "evidence_extraction")
        if len(extractions) > 1:
            return self._missing("Multiple current evidence packages match this work item; reconcile them before continuing.")

        research = self._matching("research", "KRP-*.json", campaign_id, work_item_id)
        if len(research) == 1:
            label = "Extract Evidence" if research[0].get("status") == "approved" else "Continue Research"
            return self._found(label, "knowledge_source_research_detail",
                               {"package_id": research[0]["package_id"]}, "source_research")
        if len(research) > 1:
            return self._missing("Multiple research packages match this work item; reconcile them before continuing.")

        return self._found("Continue: Prepare Research", "knowledge_campaign_orchestration_detail",
                           {"campaign_id": campaign_id}, "campaign_orchestration")

    def _matching(self, directory: str, pattern: str, campaign_id: str,
                  work_item_id: str) -> list[dict[str, Any]]:
        matches = []
        for path in sorted((self.campaign_root / directory).glob(pattern)):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            package_campaign_id = value.get("campaign_id")
            package_work_item_id = value.get("work_item_id")
            if directory == "evidence_extraction":
                research = self._research_identity(value.get("research_package_id"))
                package_campaign_id = research.get("campaign_id")
                package_work_item_id = research.get("work_item_id")
            if (package_campaign_id == campaign_id
                    and package_work_item_id == work_item_id
                    and value.get("status") not in {"rejected", "superseded"}):
                matches.append(value)
        return matches

    def _research_identity(self, package_id: str | None) -> dict[str, Any]:
        if not package_id:
            return {}
        path = self.campaign_root / "research" / f"{package_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _found(label: str, endpoint: str, route_values: dict[str, str], owner: str):
        return {"resolved": True, "label": label, "endpoint": endpoint,
                "route_values": route_values, "owner": owner}

    @staticmethod
    def _missing(reason: str):
        return {"resolved": False, "label": None, "endpoint": None,
                "route_values": {}, "owner": None, "reason": reason}
