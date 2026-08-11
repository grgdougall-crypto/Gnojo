from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.knowledge_claim_planning_service import KnowledgeClaimPlanningService
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_draft_assembly_service import KnowledgeDraftAssemblyService
from app.services.knowledge_draft_generation_service import KnowledgeDraftGenerationService
from app.services.knowledge_evidence_extraction_service import KnowledgeEvidenceExtractionService
from app.services.knowledge_source_research_service import KnowledgeSourceResearchService
from app.services.knowledge_workflow_generation_service import KnowledgeWorkflowGenerationService
from app.services.campaign_review_destination_service import CampaignReviewDestinationService


class KnowledgeCampaignOrchestrationError(ValueError):
    pass


WORKFLOW_TYPES = {"workflow", "workflow_branch", "verification_step", "escalation_path", "safety_review"}

# Authority is declared centrally and is intentionally conservative. Route handlers do
# not decide whether a phase may be crossed.
ACTION_POLICY = {
    "analyze_coverage": {"authority": "machine_safe", "external": False},
    "prepare_research": {"authority": "machine_safe", "external": False},
    "run_source_research": {"authority": "machine_safe", "external": True},
    "prepare_evidence": {"authority": "machine_safe", "external": False},
    "extract_evidence": {"authority": "machine_safe", "external": True},
    "prepare_article_package": {"authority": "machine_safe", "external": False},
    "prepare_claim_plan": {"authority": "machine_safe", "external": False},
    "plan_claims": {"authority": "machine_safe", "external": False},
    "assemble_article": {"authority": "machine_safe", "external": False},
    "prepare_workflow_package": {"authority": "machine_safe", "external": False},
    "plan_workflow": {"authority": "machine_safe", "external": False},
    "prepare_workflow_draft": {"authority": "machine_safe", "external": False},
    "approve_source": {"authority": "human_gate", "external": False},
    "review_evidence": {"authority": "human_gate", "external": False},
    "review_claims": {"authority": "human_gate", "external": False},
    "review_article_draft": {"authority": "human_gate", "external": False},
    "review_workflow_draft": {"authority": "human_gate", "external": False},
    "accept_article_content_studio": {"authority": "human_gate", "external": False},
    "accept_workflow_content_studio": {"authority": "human_gate", "external": False},
    "publish": {"authority": "human_gate", "external": False},
}


class KnowledgeCampaignOrchestrationService:
    """Supervised, bounded coordination over the authoritative Phase 1-8 services."""

    def __init__(self, repository_root: Path | None = None, campaign_root: Path | None = None,
                 *, planner=None, research=None, evidence=None, generation=None,
                 claims=None, assembly=None, workflows=None, review_destinations=None, max_transitions: int = 24,
                 max_work_items: int = 12, max_external_operations: int = 1):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.package_root = self.campaign_root / "orchestration"
        self.planner = planner or KnowledgeCoveragePlannerService(self.repository_root, self.campaign_root)
        self.research = research or KnowledgeSourceResearchService(self.repository_root, self.campaign_root)
        self.evidence = evidence or KnowledgeEvidenceExtractionService(self.repository_root, self.campaign_root)
        self.generation = generation or KnowledgeDraftGenerationService(self.repository_root, self.campaign_root)
        self.claims = claims or KnowledgeClaimPlanningService(self.generation, self.campaign_root)
        self.assembly = assembly or KnowledgeDraftAssemblyService(self.generation, self.campaign_root)
        self.workflows = workflows or KnowledgeWorkflowGenerationService(self.repository_root, self.campaign_root)
        self.review_destinations = review_destinations or CampaignReviewDestinationService(self.repository_root)
        self.limits = {
            "max_transitions": max(1, int(max_transitions)),
            "max_work_items": max(1, int(max_work_items)),
            "max_external_operations": max(0, int(max_external_operations)),
            "max_retries": 0,
        }

    @staticmethod
    def action_policy() -> dict[str, dict[str, Any]]:
        return deepcopy(ACTION_POLICY)

    def get_or_create(self, campaign_id: str, mode: str = "supervised") -> dict[str, Any]:
        if mode not in {"manual", "supervised"}:
            raise KnowledgeCampaignOrchestrationError("Orchestration mode must be manual or supervised.")
        campaign = self.planner.get(campaign_id)
        orchestration_id = self._stable_id("KORCH", campaign_id)
        path = self._path(orchestration_id)
        if path.exists():
            return self.refresh(orchestration_id)
        now = self._now()
        record = {
            "schema_version": "1.0", "orchestration_id": orchestration_id,
            "campaign_id": campaign_id, "campaign_objective": campaign.get("objective", ""),
            "status": "active", "mode": mode, "work_item_states": [],
            "actionable_queue": [], "human_review_queue": [], "blockers": [],
            "stale_dependencies": [], "completed_items": [], "next_recommended_action": None,
            "pipeline_summary": {}, "readiness_summary": {}, "dependency_graph": {},
            "last_execution_at": None, "fingerprints": {}, "revisions": [],
            "history": [{"event": "orchestration_enabled", "at": now, "actor": "Human", "mode": mode}],
            "created_at": now, "updated_at": now,
        }
        self._save(record)
        return self.refresh(orchestration_id)

    def get(self, orchestration_id: str) -> dict[str, Any]:
        path = self._path(orchestration_id)
        if not path.exists():
            raise KnowledgeCampaignOrchestrationError(f"Orchestration '{orchestration_id}' was not found.")
        return self._read(path)

    def set_mode(self, orchestration_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"manual", "supervised"}:
            raise KnowledgeCampaignOrchestrationError("Orchestration mode must be manual or supervised.")
        record = self.get(orchestration_id)
        if record.get("mode") != mode:
            record["mode"] = mode
            self._event(record, "mode_changed", "Human", mode=mode)
            self._save(record)
        return self.refresh(orchestration_id)

    def refresh(self, orchestration_id: str) -> dict[str, Any]:
        record = self.get(orchestration_id)
        campaign = self.planner.get(record["campaign_id"])
        states = [self._resolve_item(campaign, item) for item in campaign.get("work_items") or []]
        projection = self._projection(campaign, states)
        fingerprint = self._fingerprint(projection)
        previous = record.get("fingerprints", {}).get("projection")
        if previous != fingerprint:
            if previous:
                record.setdefault("revisions", []).append({
                    "at": self._now(), "fingerprint": previous,
                    "status": record.get("status"), "readiness_summary": record.get("readiness_summary", {}),
                })
            record.update(projection)
            record.setdefault("fingerprints", {})["projection"] = fingerprint
            record["updated_at"] = self._now()
            self._save(record)
        return deepcopy(record)

    def continue_campaign(self, orchestration_id: str) -> dict[str, Any]:
        record = self.refresh(orchestration_id)
        if record.get("mode") != "supervised":
            raise KnowledgeCampaignOrchestrationError("Continue Campaign is available only in supervised mode.")
        outcomes, transitions, external = [], 0, 0
        candidate = record.get("next_recommended_action")
        if candidate and candidate.get("action_authority") == "machine_safe":
            policy = ACTION_POLICY.get(candidate.get("next_action"), {})
            if policy.get("authority") == "machine_safe":
                if policy.get("external") and self.limits["max_external_operations"] < 1:
                    outcomes.append({"work_item_id": candidate["work_item_id"], "status": "limit_reached",
                                     "action": candidate["next_action"],
                                     "message": "External operation limit reached."})
                else:
                    outcomes.append(self._execute(
                        record["campaign_id"], candidate["work_item_id"], candidate["next_action"]
                    ))
                    transitions = 1
                    external = int(bool(policy.get("external")))
        record = self.refresh(orchestration_id)
        if outcomes:
            record["last_execution_at"] = self._now()
            self._event(record, "campaign_continued", "Human", transitions=transitions,
                        external_operations=external, outcomes=outcomes)
            self._save(record)
        result = self.refresh(orchestration_id)
        result["execution"] = {"outcomes": outcomes, "transitions": transitions,
                               "external_operations": external, "limits": deepcopy(self.limits)}
        return result

    def advance_item(self, orchestration_id: str, work_item_id: str) -> dict[str, Any]:
        record = self.refresh(orchestration_id)
        item = next((value for value in record.get("work_item_states", [])
                     if value["work_item_id"] == work_item_id), None)
        if not item:
            raise KnowledgeCampaignOrchestrationError("Campaign work item was not found.")
        if item.get("action_authority") != "machine_safe":
            raise KnowledgeCampaignOrchestrationError("This item is at a human review gate or is not actionable.")
        outcome = self._execute(record["campaign_id"], work_item_id, item["next_action"])
        record = self.refresh(orchestration_id)
        record["last_execution_at"] = self._now()
        self._event(record, "work_item_advanced", "Human", work_item_id=work_item_id, outcome=outcome)
        self._save(record)
        result = self.refresh(orchestration_id)
        result["execution"] = {"outcomes": [outcome], "transitions": 1, "limits": deepcopy(self.limits)}
        return result

    def _resolve_item(self, campaign, work):
        base = {"work_item_id": work["work_item_id"], "gap_id": work["gap_id"],
                "title": work.get("area_id", "").replace("-", " ").title(),
                "work_type": work.get("work_type"), "priority": work.get("priority", "medium"),
                "stage": "coverage_identified", "state": "ready", "next_action": None,
                "action_authority": None, "package_id": None, "review_link": None,
                "blocker": None, "dependencies": [], "stale": False}
        if campaign.get("status") == "draft" or not campaign.get("last_analyzed_at"):
            return self._action(base, "coverage_identified", "analyze_coverage")
        reuse = self._reuse_for_work(campaign, work)
        if reuse and work.get("work_type") not in WORKFLOW_TYPES:
            destination = self.review_destinations.resolve(reuse)
            if not destination.get("resolved"):
                return self._blocked(base, "reuse_target", "Reuse identity",
                                     destination.get("reason", "The reuse target could not be resolved."),
                                     "Reconcile the reuse opportunity with an existing governed resource.")
            base.update(stage="reuse_available", state="complete", next_action=None,
                        package_id=destination["resource_id"], dependencies=[destination["resource_id"]],
                        review_destination=destination,
                        reuse={"opportunity_id": reuse.get("opportunity_id"),
                               "article_id": reuse.get("article_id"),
                               "workflow_ids": list(reuse.get("workflow_ids") or [])})
            return base
        if work.get("work_type") in WORKFLOW_TYPES:
            return self._resolve_workflow(campaign, work, base)
        return self._resolve_article(campaign, work, base)

    @staticmethod
    def _reuse_for_work(campaign, work):
        candidates = list(campaign.get("reuse_opportunities") or [])
        explicit = work.get("reuse_opportunity_id")
        if explicit:
            return next((item for item in candidates if item.get("opportunity_id") == explicit), None)
        target = work.get("target_asset")
        if target:
            return next((item for item in candidates if target in {
                item.get("article_id"), item.get("workflow_id"), item.get("target_asset")}), None)
        evidence = set(work.get("evidence") or [])
        evidence_matches = [item for item in candidates if evidence.intersection(item.get("evidence") or [])]
        if len(evidence_matches) == 1:
            return evidence_matches[0]
        area_matches = [item for item in candidates if work.get("area_id") in (item.get("areas") or [])]
        return area_matches[0] if len(area_matches) == 1 else None

    def _resolve_workflow(self, campaign, work, base):
        packages = [item for item in self.workflows.list_for_campaign(campaign["campaign_id"])
                    if item.get("work_item_id") == work["work_item_id"]]
        if not packages:
            eligibility = self.workflows.eligibility(campaign["campaign_id"], work["work_item_id"])
            if not eligibility.get("eligible"):
                reasons = " ".join(str(item) for item in eligibility.get("reasons") or [])
                return self._blocked(
                    base, "workflow_eligibility", "Workflow generation",
                    reasons or "Phase 8 workflow-generation prerequisites are not satisfied.",
                    "Prepare and approve the required structured workflow claims before creating a workflow package.",
                )
            return self._action(base, "workflow_planning_ready", "prepare_workflow_package")
        package = packages[0]
        base.update(package_id=package["generation_id"], dependencies=[package["generation_id"]],
                    review_link=f"/curator/growth/workflow-generation/{package['generation_id']}")
        status = package.get("effective_status") or package.get("status")
        if status == "stale":
            return self._blocked(base, "stale", "Workflow generation", "Upstream inputs changed.", "Refresh the workflow package.", stale=True)
        if status == "prepared": return self._action(base, "workflow_planning_ready", "plan_workflow")
        if status == "plan_ready": return self._action(base, "workflow_draft_ready", "prepare_workflow_draft")
        if status == "draft_ready": return self._gate(base, "draft_review_required", "review_workflow_draft")
        if status == "approved_for_handoff": return self._gate(base, "content_studio_ready", "accept_workflow_content_studio")
        if status == "handed_off":
            base.update(stage="content_studio_ready", state="complete", next_action=None)
            return base
        if status in {"needs_revision", "rejected"}:
            return self._blocked(base, "workflow_validation", "Workflow generation", "Workflow draft requires revision.", "Review the workflow package.")
        return self._blocked(base, "workflow_state", "Workflow generation", f"Workflow package is in '{status}'.", "Review the authoritative package.")

    def _resolve_article(self, campaign, work, base):
        research = [item for item in self.research.list_for_campaign(campaign["campaign_id"])
                    if item.get("work_item_id") == work["work_item_id"]]
        if not research:
            return self._action(base, "research_needed", "prepare_research")
        rp = research[0]
        base.update(package_id=rp["package_id"], dependencies=[rp["package_id"]],
                    review_link=f"/curator/growth/source-research/{rp['package_id']}")
        if rp.get("status") in {"pending", "researching"}:
            return self._action(base, "research_needed", "run_source_research")
        if rp.get("status") == "ready_for_review":
            return self._gate(base, "source_approval_required", "approve_source")
        if rp.get("status") in {"needs_refresh", "rejected", "archived"}:
            return self._blocked(base, "source_state", "Source research", f"Research is {rp.get('status') }.", "Review or refresh the research package.", stale=rp.get("status") == "needs_refresh")
        if rp.get("status") != "approved":
            return self._blocked(base, "source_state", "Source research", "Research has not reached an approved state.", "Review the research package.")
        selected = list(rp.get("selected_sources") or [])
        extractions = self.evidence.list_for_research(rp["package_id"])
        missing = next((source for source in selected if not any(item.get("source_candidate_id") == source for item in extractions)), None)
        if missing:
            base["source_candidate_id"] = missing
            return self._action(base, "evidence_extraction_ready", "prepare_evidence")
        proposed = next((item for item in extractions if item.get("status") == "proposed"), None)
        if proposed:
            base.update(package_id=proposed["extraction_id"], dependencies=base["dependencies"] + [proposed["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{proposed['extraction_id']}")
            return self._action(base, "evidence_extraction_ready", "extract_evidence")
        stale = next((item for item in extractions if item.get("status") in {"needs_refresh", "failed"}), None)
        if stale:
            base.update(package_id=stale["extraction_id"], dependencies=base["dependencies"] + [stale["extraction_id"]])
            return self._blocked(base, "evidence_state", "Evidence extraction", f"Evidence is {stale.get('status')}.", "Refresh or inspect the evidence package.", stale=stale.get("status") == "needs_refresh")
        pending = next((item for item in extractions if item.get("status") in {"retrieving", "needs_review", "partially_approved", "extracted"}), None)
        if pending:
            base.update(package_id=pending["extraction_id"], dependencies=base["dependencies"] + [pending["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{pending['extraction_id']}")
            return self._gate(base, "evidence_review_required", "review_evidence")
        drafts = [item for item in self.generation.list_for_campaign(campaign["campaign_id"])
                  if item.get("work_item_id") == work["work_item_id"]]
        if not drafts:
            return self._action(base, "claim_planning_ready", "prepare_article_package")
        draft = drafts[0]
        base.update(package_id=draft["package_id"], dependencies=base["dependencies"] + [draft["package_id"]],
                    review_link=f"/curator/growth/draft-generation/{draft['package_id']}")
        plans = self.claims.list_for_kdg(draft["package_id"])
        if not plans:
            return self._action(base, "claim_planning_ready", "prepare_claim_plan")
        plan = plans[0]
        base["dependencies"].append(plan["claim_plan_id"])
        base["review_link"] = f"/curator/growth/claim-planning/{plan['claim_plan_id']}"
        if plan.get("status") == "proposed": return self._action(base, "claim_planning_ready", "plan_claims")
        if plan.get("status") in {"planned", "needs_review", "partially_approved"}:
            return self._gate(base, "claim_review_required", "review_claims")
        if plan.get("status") in {"needs_evidence", "conflicted", "rejected", "superseded"}:
            return self._blocked(base, "claim_state", "Claim planning", f"Claim plan is {plan.get('status')}.", "Resolve evidence gaps or conflicts in the claim workspace.", stale=plan.get("status") == "needs_evidence")
        assemblies = self.assembly.list_for_kdg(draft["package_id"])
        if plan.get("status") == "ready_for_drafting" and not assemblies:
            return self._action(base, "article_assembly_ready", "assemble_article")
        assembly = assemblies[0] if assemblies else None
        if assembly:
            base.update(package_id=assembly["assembly_id"], dependencies=base["dependencies"] + [assembly["assembly_id"]],
                        review_link=f"/curator/growth/draft-assembly/{assembly['assembly_id']}")
            status = assembly.get("status")
            if status == "ready_for_review": return self._gate(base, "draft_review_required", "review_article_draft")
            if status == "handed_off":
                base.update(stage="content_studio_ready", state="complete", next_action=None)
                return base
            return self._blocked(base, "assembly_state", "Draft assembly", f"Assembly is {status}.", "Review the assembly and validation results.", stale=status == "stale")
        return self._blocked(base, "article_state", "Article pipeline", "No eligible article action is currently available.", "Review the authoritative packages.")

    def _execute(self, campaign_id, work_item_id, action):
        try:
            if action == "analyze_coverage":
                self.planner.analyze(campaign_id)
                return {"work_item_id": work_item_id, "action": action, "status": "completed"}
            campaign = self.planner.get(campaign_id)
            work = next(item for item in campaign.get("work_items", []) if item["work_item_id"] == work_item_id)
            if action == "prepare_research": self.research.create(campaign_id, work["gap_id"], work_item_id)
            elif action == "run_source_research":
                package = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.research.run(package["package_id"])
            elif action == "prepare_evidence":
                package = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                missing = next(source for source in package["selected_sources"] if not any(
                    item.get("source_candidate_id") == source for item in self.evidence.list_for_research(package["package_id"])))
                self.evidence.prepare(package["package_id"], missing)
            elif action == "extract_evidence":
                rp = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                package = next(item for item in self.evidence.list_for_research(rp["package_id"]) if item.get("status") == "proposed")
                self.evidence.extract(package["extraction_id"])
            elif action == "prepare_article_package": self.generation.prepare(campaign_id, work["gap_id"], work_item_id)
            elif action == "prepare_claim_plan":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.claims.prepare(package["package_id"])
            elif action == "plan_claims":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.claims.plan(self.claims.list_for_kdg(package["package_id"])[0]["claim_plan_id"])
            elif action == "assemble_article":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.assembly.assemble(self.claims.list_for_kdg(package["package_id"])[0]["claim_plan_id"])
            elif action == "prepare_workflow_package": self.workflows.prepare(campaign_id, work_item_id)
            elif action == "plan_workflow":
                package = next(item for item in self.workflows.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.workflows.plan(package["generation_id"])
            elif action == "prepare_workflow_draft":
                package = next(item for item in self.workflows.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.workflows.prepare_draft(package["generation_id"])
            else: raise KnowledgeCampaignOrchestrationError(f"Action '{action}' is not machine-safe.")
            return {"work_item_id": work_item_id, "action": action, "status": "completed"}
        except Exception as error:  # isolate one phase failure from independent work
            return {"work_item_id": work_item_id, "action": action, "status": "failed",
                    "error_type": type(error).__name__, "message": str(error), "retry_eligible": False,
                    "at": self._now()}

    def _projection(self, campaign, states):
        unique = []
        seen = set()
        for item in states:
            identity = (item.get("work_item_id"), (item.get("reuse") or {}).get("opportunity_id"),
                        item.get("stage"), item.get("package_id"))
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        states = unique
        actionable = [item for item in states if item.get("action_authority") == "machine_safe"]
        if (campaign.get("status") == "draft" or not campaign.get("last_analyzed_at")) and not actionable:
            actionable.append({
                "work_item_id": "__campaign__", "gap_id": None,
                "title": "Analyze campaign coverage", "work_type": "campaign",
                "priority": "medium", "stage": "coverage_identified",
                "state": "machine_ready", "next_action": "analyze_coverage",
                "action_authority": "machine_safe", "package_id": None,
                "review_link": None, "blocker": None, "dependencies": [], "stale": False,
            })
        review = [self._review_entry(item) for item in states if item.get("action_authority") == "human_gate"]
        blockers = [item["blocker"] for item in states if item.get("blocker")]
        stale = [self._stale_entry(item) for item in states if item.get("stale")]
        completed = [item["work_item_id"] for item in states if item.get("state") == "complete"]
        counts = {key: sum(1 for item in states if item.get("stage") == key) for key in {
            "coverage_identified", "research_needed", "source_approval_required", "evidence_extraction_ready",
            "evidence_review_required", "claim_planning_ready", "claim_review_required", "article_assembly_ready",
            "workflow_planning_ready", "workflow_draft_ready", "draft_review_required", "content_studio_ready", "blocked", "stale"}}
        total = len(states)
        status = "completed" if total and len(completed) == total and not blockers and not stale else (
            "awaiting_human_review" if review else "blocked" if blockers and not actionable else "active")
        next_action = (actionable[0] if actionable else review[0] if review else blockers[0] if blockers else None)
        return {
            "campaign_objective": campaign.get("objective", ""), "status": status,
            "work_item_states": states, "actionable_queue": actionable, "human_review_queue": review,
            "blockers": blockers, "stale_dependencies": stale, "completed_items": completed,
            "next_recommended_action": next_action, "pipeline_summary": counts,
            "readiness_summary": {"total": total, "completed": len(completed), "machine_ready": len(actionable),
                                  "human_review": len(review), "blocked": len(blockers), "stale": len(stale),
                                  "content_studio_ready": sum(1 for item in states if item.get("stage") == "content_studio_ready"),
                                  "completion_percent": round(100 * len(completed) / total) if total else 0},
            "dependency_graph": self._graph(campaign, states),
        }

    @staticmethod
    def _graph(campaign, states):
        nodes = [{"id": campaign["campaign_id"], "type": "campaign", "label": campaign.get("title", "Campaign")}]
        edges = []
        for item in states:
            nodes.append({"id": item["work_item_id"], "type": "work_item", "label": item["title"]})
            edges.append({"from": campaign["campaign_id"], "to": item["work_item_id"]})
            parent = item["work_item_id"]
            for dependency in item.get("dependencies", []):
                nodes.append({"id": dependency, "type": dependency.split("-", 1)[0], "label": dependency})
                edges.append({"from": parent, "to": dependency})
                parent = dependency
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _action(base, stage, action):
        base.update(stage=stage, state="machine_ready", next_action=action,
                    action_authority=ACTION_POLICY[action]["authority"])
        return base

    @staticmethod
    def _gate(base, stage, action):
        base.update(stage=stage, state="awaiting_human_review", next_action=action,
                    action_authority=ACTION_POLICY[action]["authority"])
        return base

    def _blocked(self, base, kind, subsystem, explanation, resolution, stale=False):
        base.update(stage="stale" if stale else "blocked", state="blocked", next_action=None,
                    action_authority=None, stale=stale, blocker={
                        "blocker_type": kind, "work_item_id": base["work_item_id"], "originating_subsystem": subsystem,
                        "original_package_id": base.get("package_id"), "severity": "medium",
                        "explanation": explanation, "recommended_resolution": resolution, "timestamp": self._now()})
        return base

    @staticmethod
    def _review_entry(item):
        return {"work_item_id": item["work_item_id"], "title": item["title"], "phase": item["stage"],
                "action": item["next_action"], "why": "A governed approval boundary has been reached.",
                "risk": item.get("priority", "medium"), "provenance": item.get("package_id"),
                "review_link": item.get("review_link")}

    @staticmethod
    def _stale_entry(item):
        return {"work_item_id": item["work_item_id"], "changed": item.get("package_id"),
                "downstream_impact": list(item.get("dependencies") or []),
                "explanation": item.get("blocker", {}).get("explanation")}

    def _path(self, orchestration_id):
        if not orchestration_id.startswith("KORCH-") or not orchestration_id[6:].isalnum():
            raise KnowledgeCampaignOrchestrationError("Invalid orchestration ID.")
        return self.package_root / f"{orchestration_id}.json"

    def _save(self, record):
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(record["orchestration_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeCampaignOrchestrationError(f"Unable to read orchestration state: {error}") from error

    @staticmethod
    def _stable_id(prefix, *parts):
        return f"{prefix}-{hashlib.sha256('|'.join(str(item) for item in parts).encode()).hexdigest()[:12].upper()}"

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _now(): return datetime.now(timezone.utc).isoformat()

    def _event(self, record, event, actor, **details):
        record.setdefault("history", []).append({"event": event, "at": self._now(), "actor": actor, **details})
        record["updated_at"] = self._now()
