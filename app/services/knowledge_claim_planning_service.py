from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.services.knowledge_draft_generation_service import (
    KnowledgeDraftGenerationError,
    KnowledgeDraftGenerationService,
)


PLAN_STATUSES = (
    "proposed", "planning", "needs_evidence", "needs_conflict_resolution",
    "needs_review", "partially_approved", "ready_for_drafting", "rejected",
    "superseded",
)
CLAIM_REVIEW_STATES = ("proposed", "approved", "rejected", "needs_revision")
SECTION_REVIEW_STATES = ("proposed", "approved", "rejected", "needs_revision")


class KnowledgeClaimPlanningError(ValueError):
    pass


class KnowledgeClaimPlanningService:
    """Human-initiated, deterministic mapping of approved evidence to claims."""

    SECTION_ORDER = (
        "purpose", "symptoms", "prerequisites", "safety", "procedure",
        "commands", "verification", "expected_result", "alternate_outcomes",
        "escalation", "platform_applicability", "related_knowledge", "sources",
    )
    REQUIRED = {"purpose", "procedure", "verification", "sources"}
    EVIDENCE_SECTION = {
        "preconditions": "prerequisites", "symptoms": "symptoms",
        "diagnostic_observations": "procedure", "procedure": "procedure",
        "commands": "commands", "expected_result": "expected_result",
        "verification": "verification", "alternate_outcomes": "alternate_outcomes",
        "safety": "safety", "authorization_requirements": "safety",
        "escalation": "escalation", "platform_applicability": "platform_applicability",
        "limitations": "alternate_outcomes",
    }
    CLAIM_TYPE = {
        "preconditions": "prerequisite", "symptoms": "symptom",
        "diagnostic_observations": "diagnostic_observation", "procedure": "action_procedure",
        "commands": "command", "expected_result": "expected_result",
        "verification": "verification", "alternate_outcomes": "alternate_outcome",
        "safety": "caution", "authorization_requirements": "authorization_requirement",
        "escalation": "escalation", "platform_applicability": "applicability",
        "limitations": "limitation",
    }

    def __init__(self, generation: KnowledgeDraftGenerationService | None = None,
                 campaign_root: Path | None = None):
        self.generation = generation or KnowledgeDraftGenerationService()
        self.campaign_root = (campaign_root or self.generation.campaign_root).resolve()
        self.package_root = self.campaign_root / "claim_planning"

    def list_for_kdg(self, package_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        values = [self._read(path) for path in self.package_root.glob("KCPM-*.json")]
        return sorted((value for value in values if value.get("kdg_package_id") == package_id),
                      key=lambda value: value.get("created_at", ""), reverse=True)

    def list_for_work(self, campaign_id: str, work_item_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        values = [self._read(path) for path in self.package_root.glob("KCPM-*.json")]
        return sorted((value for value in values
                       if value.get("campaign_id") == campaign_id
                       and value.get("work_item_id") == work_item_id),
                      key=lambda value: value.get("created_at", ""), reverse=True)

    def get(self, plan_id: str) -> dict[str, Any]:
        path = self._path(plan_id)
        if not path.exists():
            raise KnowledgeClaimPlanningError(f"Claim plan '{plan_id}' was not found.")
        return self._with_current_evidence_state(self._read(path))

    def prepare(self, package_id: str) -> dict[str, Any]:
        package, evidence = self._eligible(package_id)
        plan_id = self._stable_id("KCPM", package_id)
        if self._path(plan_id).exists():
            return self.get(plan_id)
        now = self.generation._now()
        plan = {
            "schema_version": "1.0", "claim_plan_id": plan_id,
            "kdg_package_id": package_id, "campaign_id": package["campaign_id"],
            "gap_id": package["gap_id"], "work_item_id": package["work_item_id"],
            "kex_package_ids": sorted({(unit.get("provenance") or {}).get("extraction_id")
                                       for unit in evidence if (unit.get("provenance") or {}).get("extraction_id")}),
            "approved_evidence_ids": sorted(unit["evidence_id"] for unit in evidence),
            "target_asset_type": package["requested_asset_type"],
            "article_identity": package["canonical_identity"],
            "article_title": package["proposed_title"], "status": "proposed",
            "sections": [], "claims": [], "conflicts": [], "evidence_gaps": [],
            "canonical_reuse": [], "validation": {}, "reviewer_notes": "",
            "input_fingerprint": None, "created_at": now, "updated_at": now,
            "history": [{"event": "claim_plan_prepared", "at": now, "actor": "Human"}],
            "revisions": [],
        }
        self._save(plan)
        package["claim_plan_id"] = plan_id
        package["updated_at"] = now
        self.generation._save(package)
        return deepcopy(plan)

    def prepare_workflow(self, campaign_id: str, work_item_id: str) -> dict[str, Any]:
        package, evidence = self._eligible_workflow(campaign_id, work_item_id)
        plan_id = self._stable_id("KCPM", campaign_id, work_item_id, "workflow")
        if self._path(plan_id).exists():
            return self.get(plan_id)
        now = self.generation._now()
        plan = {
            "schema_version": "1.0", "claim_plan_id": plan_id,
            "kdg_package_id": None, "campaign_id": campaign_id,
            "gap_id": package["gap_id"], "work_item_id": work_item_id,
            "kex_package_ids": self._extraction_ids(evidence),
            "approved_evidence_ids": sorted(unit["evidence_id"] for unit in evidence),
            "target_asset_type": "workflow", "workflow_identity": package["workflow_identity"],
            "workflow_name": package["workflow_name"], "article_title": package["workflow_name"],
            "status": "proposed",
            "sections": [], "claims": [], "conflicts": [], "evidence_gaps": [],
            "canonical_reuse": [], "validation": {}, "reviewer_notes": "",
            "input_fingerprint": None, "created_at": now, "updated_at": now,
            "history": [{"event": "workflow_claim_plan_prepared", "at": now, "actor": "Human"}],
            "revisions": [],
        }
        self._save(plan)
        return deepcopy(plan)

    def plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.get(plan_id)
        if plan.get("target_asset_type") == "workflow":
            package, evidence = self._eligible_workflow(plan["campaign_id"], plan["work_item_id"])
        else:
            package, evidence = self._eligible(plan["kdg_package_id"])
        fingerprint = self._evidence_input_fingerprint(evidence)
        if plan.get("input_fingerprint") == fingerprint and plan.get("claims"):
            return plan
        now = self.generation._now()
        if plan.get("input_fingerprint") and plan.get("claims"):
            plan["revisions"].append({"input_fingerprint": plan["input_fingerprint"],
                                      "claims": deepcopy(plan["claims"]),
                                      "sections": deepcopy(plan["sections"]),
                                      "superseded_at": now})
        prior = {claim["claim_id"]: claim for claim in plan.get("claims") or []}
        claims = self._claims(package, evidence, prior)
        if plan.get("target_asset_type") == "workflow":
            claims = self._workflow_specs(package, claims)
        prior_conflicts = {item["conflict_id"]: item for item in plan.get("conflicts") or []}
        conflicts = self._conflicts(claims)
        for conflict in conflicts:
            previous = prior_conflicts.get(conflict["conflict_id"], {})
            conflict.update({key: previous.get(key) for key in (
                "resolution", "reviewer_notes", "reviewed_at"
            )})
        reuse = self._canonical_reuse(package)
        prior_sections = {item["section"]: item for item in plan.get("sections") or []}
        sections, gaps = self._sections(package, claims, conflicts, reuse, prior_sections)
        if plan.get("target_asset_type") == "workflow":
            procedure_coverage, procedure_gaps = self._workflow_procedure_coverage(
                package, claims
            )
            gaps.extend(procedure_gaps)
            gaps.extend(self._workflow_spec_gaps(package, claims))
        else:
            procedure_coverage = None
        plan.update({
            "status": self._status(claims, conflicts, gaps, sections), "sections": sections,
            "claims": claims, "conflicts": conflicts, "evidence_gaps": gaps,
            "canonical_reuse": reuse, "approved_evidence_ids": sorted(unit["evidence_id"] for unit in evidence),
            "kex_package_ids": sorted({(unit.get("provenance") or {}).get("extraction_id")
                                       for unit in evidence if (unit.get("provenance") or {}).get("extraction_id")}),
            "input_fingerprint": fingerprint, "updated_at": now,
            "validation": {"approved_evidence_only": True,
                           "blocking_conflicts": len([item for item in conflicts
                                                      if item.get("resolution") in {None, "", "deferred"}]),
                           "required_gaps": len([item for item in gaps if item["required"]]),
                           "workflow_procedure_coverage": procedure_coverage,
                           "substantive_completeness_percent": (
                               round(100 * len(procedure_coverage["covered"]) /
                                     len(procedure_coverage["required"]))
                               if procedure_coverage and procedure_coverage["required"] else None
                           )},
        })
        self._event(plan, "claim_plan_built", now, actor="Deterministic Claim Planner",
                    claim_count=len(claims), conflict_count=len(conflicts), gap_count=len(gaps))
        self._save(plan)
        return self.get(plan_id)

    def input_is_current(self, plan_id: str) -> bool:
        """Return whether a persisted plan represents current approved evidence."""
        plan = self._read(self._path(plan_id))
        if plan.get("target_asset_type") == "workflow":
            _, evidence = self._eligible_workflow(
                plan["campaign_id"], plan["work_item_id"]
            )
        else:
            _, evidence = self._eligible(plan["kdg_package_id"])
        return bool(
            plan.get("input_fingerprint")
            and plan["input_fingerprint"] == self._evidence_input_fingerprint(evidence)
        )

    def review_workspace(self, plan_id: str) -> dict[str, Any]:
        """Project the bounded missing-workflow claim-set review without writes."""
        plan = self.get(plan_id)
        compression = self._claim_review_compression(plan)
        return {"plan": plan, "compression": compression}

    def approve_reviewed_claim_set(
        self, plan_id: str, *, expected_plan_fingerprint: str,
        expected_evidence_fingerprint: str, reviewer: str,
    ) -> dict[str, Any]:
        """Atomically approve one exact, current, human-reviewed workflow claim set."""
        path = self._path(plan_id)
        before_bytes = path.read_bytes()
        plan = self.get(plan_id)
        compression = self._claim_review_compression(plan)
        prior_approval = plan.get("claim_set_approval") or {}
        if (
            prior_approval.get("plan_fingerprint") == expected_plan_fingerprint
            and prior_approval.get("evidence_input_fingerprint")
            == expected_evidence_fingerprint
            and prior_approval.get("decision") == "approved"
            and self.input_is_current(plan_id)
        ):
            result = deepcopy(plan)
            result["claim_set_approval_result"] = "already_approved"
            return result
        if not compression.get("enabled"):
            raise KnowledgeClaimPlanningError(
                "Claim-set approval is limited to governed missing-workflow plans."
            )
        if not self.input_is_current(plan_id):
            raise KnowledgeClaimPlanningError(
                "Approved evidence changed. Re-plan before reviewing this claim set."
            )
        if expected_evidence_fingerprint != plan.get("input_fingerprint"):
            raise KnowledgeClaimPlanningError(
                "The reviewed evidence fingerprint is stale."
            )
        if expected_plan_fingerprint != compression.get("plan_fingerprint"):
            raise KnowledgeClaimPlanningError(
                "The reviewed claim-set fingerprint is stale."
            )
        if not compression.get("ready_for_package_approval"):
            reasons = compression.get("blocking_reasons") or [
                "Resolve every claim exception, conflict, and evidence gap first."
            ]
            raise KnowledgeClaimPlanningError(" ".join(reasons))
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise KnowledgeClaimPlanningError("Reviewer identity is required.")
        # Recheck the exact persisted bytes immediately before the single write.
        if path.read_bytes() != before_bytes:
            raise KnowledgeClaimPlanningError(
                "The claim plan changed during review. Reload and try again."
            )
        now = self.generation._now()
        approved_plan = deepcopy(plan)
        for claim in approved_plan.get("claims") or []:
            if claim.get("review_state") == "proposed":
                claim.update(
                    review_state="approved", reviewed_at=now,
                    reviewer_notes="Approved in governed claim-set review.",
                    reviewed_by=reviewer,
                )
        for section in approved_plan.get("sections") or []:
            if section.get("claim_ids") and self._section_is_package_approvable(section):
                section.update(
                    review_state="approved", reviewed_at=now,
                    reviewer_notes="Approved in governed claim-set review.",
                    reviewed_by=reviewer,
                )
        approved_plan["status"] = self._status(
            approved_plan.get("claims") or [], approved_plan.get("conflicts") or [],
            approved_plan.get("evidence_gaps") or [], approved_plan.get("sections") or [],
        )
        if approved_plan["status"] != "ready_for_drafting":
            raise KnowledgeClaimPlanningError(
                "The reviewed claim set cannot reach the governed drafting boundary: "
                + "; ".join(self._drafting_boundary_blockers(approved_plan))
            )
        approved_plan["claim_set_approval"] = {
            "decision": "approved", "reviewer": reviewer, "approved_at": now,
            "plan_fingerprint": expected_plan_fingerprint,
            "evidence_input_fingerprint": expected_evidence_fingerprint,
        }
        approved_plan["updated_at"] = now
        self._event(
            approved_plan, "reviewed_claim_set_approved", now, actor=reviewer,
            plan_fingerprint=expected_plan_fingerprint,
            evidence_input_fingerprint=expected_evidence_fingerprint,
            claim_count=len(approved_plan.get("claims") or []),
        )
        self._save(approved_plan)
        result = self.get(plan_id)
        result["claim_set_approval_result"] = "approved"
        return result

    def _claim_review_compression(self, plan: dict[str, Any]) -> dict[str, Any]:
        enabled = False
        if plan.get("target_asset_type") == "workflow":
            try:
                package, _ = self._eligible_workflow(
                    plan["campaign_id"], plan["work_item_id"], allow_conflicts=True
                )
                enabled = package.get("gap_type") == "missing_workflow"
            except KnowledgeClaimPlanningError:
                enabled = False
        if not enabled:
            return {"enabled": False}
        conflict_claims = {
            claim_id for conflict in plan.get("conflicts") or []
            if conflict.get("resolution") in {None, "", "deferred"}
            for claim_id in conflict.get("claim_ids") or []
        }
        exceptions = []
        routine = []
        for claim in plan.get("claims") or []:
            reasons = []
            if claim.get("stale"):
                reasons.append("stale evidence")
            if claim.get("claim_id") in conflict_claims:
                reasons.append("deterministic conflict")
            if claim.get("confidence") == "low" or claim.get("support_level") == "partial":
                reasons.append("weak support")
            if claim.get("claim_type") in {
                "command", "caution", "authorization_requirement"
            }:
                reasons.append("safety or state-changing review")
            if not claim.get("applicability"):
                reasons.append("platform applicability is unresolved")
            if claim.get("support_level") == "conditional" and (
                not claim.get("applicability") or claim.get("limitations")
            ):
                reasons.append("conditional applicability is unresolved")
            if claim.get("review_state") in {"rejected", "needs_revision"}:
                reasons.append("prior human decision requires resolution")
            projected = deepcopy(claim)
            projected["attention_reasons"] = self._unique(reasons)
            (exceptions if reasons else routine).append(projected)
        unresolved = [
            claim for claim in exceptions if claim.get("review_state") != "approved"
        ]
        plan_fingerprint = self._fingerprint({
            "claim_plan_id": plan.get("claim_plan_id"),
            "input_fingerprint": plan.get("input_fingerprint"),
            "claims": [{key: claim.get(key) for key in (
                "claim_id", "normalized_claim", "evidence_ids", "workflow_coverage_roles",
                "support_level", "confidence", "applicability", "limitations", "review_state",
            )} for claim in plan.get("claims") or []],
            "sections": [{key: section.get(key) for key in (
                "section", "claim_ids", "review_state", "missing_evidence",
            )} for section in plan.get("sections") or []],
            "conflicts": plan.get("conflicts") or [],
            "evidence_gaps": plan.get("evidence_gaps") or [],
        })
        coverage = (plan.get("validation") or {}).get(
            "workflow_procedure_coverage"
        ) or {"required": [], "covered": [], "missing": []}
        section_blockers = self._package_section_blockers(plan)
        blocking_reasons = []
        if unresolved:
            blocking_reasons.append(
                f"{len(unresolved)} claim exception(s) still require review."
            )
        if plan.get("conflicts"):
            blocking_reasons.append("Deterministic claim conflicts remain unresolved.")
        if plan.get("evidence_gaps"):
            blocking_reasons.append("Required evidence gaps remain.")
        if (plan.get("validation") or {}).get("stale_evidence_ids"):
            blocking_reasons.append("The claim plan references stale evidence.")
        blocking_reasons.extend(section_blockers)
        return {
            "enabled": True,
            "plan_fingerprint": plan_fingerprint,
            "evidence_input_fingerprint": plan.get("input_fingerprint"),
            "total_claims": len(plan.get("claims") or []),
            "direct_high_confidence": sum(
                claim.get("support_level") == "direct"
                and claim.get("confidence") == "high"
                for claim in plan.get("claims") or []
            ),
            "conditional_claims": sum(
                claim.get("support_level") == "conditional"
                for claim in plan.get("claims") or []
            ),
            "attention_count": len(exceptions),
            "unresolved_attention_count": len(unresolved),
            "routine_claims": routine,
            "exception_claims": exceptions,
            "procedure_coverage": coverage,
            "verification_coverage": "success_verification" in set(
                coverage.get("covered") or []
            ),
            "conflict_count": len(plan.get("conflicts") or []),
            "evidence_gap_count": len(plan.get("evidence_gaps") or []),
            "section_blockers": section_blockers,
            "blocking_reasons": blocking_reasons,
            "ready_for_package_approval": bool(plan.get("claims"))
            and not unresolved
            and not plan.get("conflicts")
            and not plan.get("evidence_gaps")
            and not (plan.get("validation") or {}).get("stale_evidence_ids")
            and not section_blockers,
        }

    @staticmethod
    def _section_is_package_approvable(section: dict[str, Any]) -> bool:
        state = section.get("review_state")
        if state == "proposed":
            return True
        # Older rebuilt plans could retain a deterministic needs_evidence label
        # after their evidence gap was resolved. It is not a human decision and
        # may be normalized only when every current blocker is absent.
        return (
            state == "needs_evidence"
            and not section.get("missing_evidence")
            and not section.get("conflict_ids")
            and not section.get("reviewed_at")
        )

    @classmethod
    def _package_section_blockers(cls, plan: dict[str, Any]) -> list[str]:
        blockers = []
        for section in plan.get("sections") or []:
            if not section.get("claim_ids"):
                continue
            state = section.get("review_state")
            if state == "approved" or cls._section_is_package_approvable(section):
                continue
            name = str(section.get("section") or "unknown").replace("_", " ").title()
            blockers.append(
                f"The {name} section mapping remains {str(state or 'unknown').replace('_', ' ')}."
            )
        return blockers

    @classmethod
    def _drafting_boundary_blockers(cls, plan: dict[str, Any]) -> list[str]:
        blockers = cls._package_section_blockers(plan)
        unapproved = sum(
            claim.get("review_state") != "approved"
            for claim in plan.get("claims") or []
        )
        if unapproved:
            blockers.append(f"{unapproved} claim(s) remain unapproved.")
        if plan.get("conflicts"):
            blockers.append("Claim conflicts remain unresolved.")
        if plan.get("evidence_gaps"):
            blockers.append("Required evidence gaps remain.")
        return blockers or ["the authoritative post-approval status is not drafting-ready."]

    @classmethod
    def _evidence_input_fingerprint(cls, evidence: list[dict[str, Any]]) -> str:
        return cls._fingerprint([
            {key: unit.get(key) for key in ("evidence_id", "evidence_type", "normalized_claim",
                                             "fingerprint", "review_state",
                                             "workflow_coverage_roles")}
            for unit in sorted(evidence, key=lambda value: value["evidence_id"])
        ])

    def review_claim(self, plan_id: str, claim_id: str, decision: str, notes: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected", "needs_revision"}:
            raise KnowledgeClaimPlanningError("Unknown claim review decision.")
        plan = self.get(plan_id)
        claim = next((item for item in plan.get("claims") or [] if item["claim_id"] == claim_id), None)
        if claim is None:
            raise KnowledgeClaimPlanningError("Planned claim was not found.")
        notes = str(notes or "").strip()
        if claim.get("review_state") == decision and claim.get("reviewer_notes", "") == notes:
            return plan
        claim.update({"review_state": decision, "reviewer_notes": notes,
                      "reviewed_at": self.generation._now()})
        plan["status"] = self._status(plan["claims"], plan.get("conflicts") or [],
                                      plan.get("evidence_gaps") or [], plan.get("sections") or [])
        plan["updated_at"] = self.generation._now()
        self._event(plan, f"claim_{decision}", plan["updated_at"], actor="Human", claim_id=claim_id)
        self._save(plan)
        return deepcopy(plan)

    def review_section(self, plan_id: str, section_name: str, decision: str,
                       notes: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected", "needs_revision"}:
            raise KnowledgeClaimPlanningError("Unknown section review decision.")
        plan = self.get(plan_id)
        section = next((item for item in plan.get("sections") or []
                        if item["section"] == section_name), None)
        if section is None or not section.get("applicable"):
            raise KnowledgeClaimPlanningError("Applicable article section was not found.")
        notes = str(notes or "").strip()
        if section.get("review_state") == decision and section.get("reviewer_notes", "") == notes:
            return plan
        section.update({"review_state": decision, "reviewer_notes": notes,
                        "reviewed_at": self.generation._now()})
        plan["status"] = self._status(plan["claims"], plan.get("conflicts") or [],
                                      plan.get("evidence_gaps") or [], plan["sections"])
        plan["updated_at"] = self.generation._now()
        self._event(plan, f"section_{decision}", plan["updated_at"], actor="Human",
                    section=section_name)
        self._save(plan)
        return deepcopy(plan)

    def review_conflict(self, plan_id: str, conflict_id: str, decision: str, notes: str = ""):
        plan = self.get(plan_id)
        conflict = next((item for item in plan.get("conflicts") or []
                         if item["conflict_id"] == conflict_id), None)
        if conflict is None:
            raise KnowledgeClaimPlanningError("Evidence conflict was not found.")
        conflict.update({"resolution": str(decision or "").strip(),
                         "reviewer_notes": str(notes or "").strip(),
                         "reviewed_at": self.generation._now()})
        plan["status"] = self._status(plan["claims"], plan["conflicts"], plan["evidence_gaps"],
                                      plan.get("sections") or [])
        plan["updated_at"] = self.generation._now()
        self._event(plan, "conflict_reviewed", plan["updated_at"], actor="Human",
                    conflict_id=conflict_id)
        self._save(plan)
        return deepcopy(plan)

    def approved_claims_for(self, package_id: str) -> list[dict[str, Any]]:
        plans = self.list_for_kdg(package_id)
        if not plans:
            return []
        plan = plans[0]
        # An incomplete plan remains a planning artifact. Even individually approved
        # claims do not cross the Phase 3/4 boundary until the whole plan is ready.
        if plan.get("status") != "ready_for_drafting":
            return []
        try:
            package = self.generation.get(package_id)
        except KnowledgeDraftGenerationError:
            return []
        current_ids = {unit["evidence_id"] for unit in self.generation.extraction.approved_units_for(
            package.get("research_package_ids") or []
        )}
        return [deepcopy(item) for item in plan.get("claims") or []
                if item.get("review_state") == "approved"
                and not item.get("stale")
                and set(item.get("evidence_ids") or []).issubset(current_ids)]

    def is_eligible(self, package_id: str) -> bool:
        try:
            self._eligible(package_id)
            return True
        except KnowledgeClaimPlanningError:
            return False

    def workflow_is_eligible(self, campaign_id: str, work_item_id: str) -> bool:
        """Read-only eligibility check for the supervised workflow claim-planning gate."""
        try:
            self._eligible_workflow(campaign_id, work_item_id)
            return True
        except KnowledgeClaimPlanningError:
            return False

    def _with_current_evidence_state(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Decorate a stored plan with live Phase 5 evidence state without mutating it."""
        value = deepcopy(plan)
        try:
            if value.get("target_asset_type") == "workflow":
                _, evidence = self._eligible_workflow(value["campaign_id"], value["work_item_id"],
                                                      allow_conflicts=True)
                current_ids = {unit["evidence_id"] for unit in evidence}
            else:
                package = self.generation.get(value["kdg_package_id"])
                current_ids = {unit["evidence_id"] for unit in self.generation.extraction.approved_units_for(
                    package.get("research_package_ids") or []
                )}
        except (KnowledgeDraftGenerationError, KnowledgeClaimPlanningError, KeyError):
            current_ids = set()
        stale_ids = set()
        for claim in value.get("claims") or []:
            missing = sorted(set(claim.get("evidence_ids") or []) - current_ids)
            claim["stale"] = bool(missing)
            claim["stale_evidence_ids"] = missing
            stale_ids.update(missing)
        for section in value.get("sections") or []:
            section_claims = [claim for claim in value.get("claims") or []
                              if claim.get("claim_id") in set(section.get("claim_ids") or [])]
            section["stale"] = any(claim.get("stale") for claim in section_claims)
        value.setdefault("validation", {})["stale_evidence_ids"] = sorted(stale_ids)
        if stale_ids and value.get("status") not in {"rejected", "superseded"}:
            value["status"] = "needs_evidence"
            stale_gap_id = self._stable_id(
                "GAP", value["claim_plan_id"], "stale_evidence"
            )
            if not any(
                gap.get("gap_id") == stale_gap_id
                for gap in value.get("evidence_gaps") or []
            ):
                value.setdefault("evidence_gaps", []).append({
                    "gap_id": stale_gap_id,
                    "section": "procedure",
                    "required": True,
                    "reason": (
                        "The plan references superseded evidence. Re-plan against "
                        "the current approved evidence before human claim review."
                    ),
                })
        completeness = value["validation"].get(
            "substantive_completeness_percent"
        )
        if not isinstance(completeness, (int, float)):
            coverage = value["validation"].get("workflow_procedure_coverage") or {}
            required = coverage.get("required") or []
            if required:
                completeness = round(
                    100 * len(coverage.get("covered") or []) / len(required)
                )
            elif stale_ids:
                completeness = 0
            else:
                required_sections = [
                    section for section in value.get("sections") or []
                    if section.get("required")
                ]
                complete_sections = [
                    section for section in required_sections
                    if not section.get("missing_evidence")
                ]
                completeness = (
                    round(100 * len(complete_sections) / len(required_sections))
                    if required_sections else 0
                )
            value["validation"]["substantive_completeness_percent"] = completeness
        return value

    def _eligible_workflow(self, campaign_id: str, work_item_id: str, *, allow_conflicts=False):
        try:
            campaign = self.generation.planner.get(campaign_id)
        except Exception as error:
            raise KnowledgeClaimPlanningError(str(error)) from error
        work = next((item for item in campaign.get("work_items") or []
                     if item.get("work_item_id") == work_item_id), None)
        if not work or work.get("work_type") not in {
            "workflow", "workflow_branch", "verification_step", "escalation_path", "safety_review"
        }:
            raise KnowledgeClaimPlanningError("Workflow claim planning requires a current workflow-oriented work item.")
        if work.get("status") in {"rejected", "superseded", "archived", "complete"}:
            raise KnowledgeClaimPlanningError("This work-item lifecycle no longer permits workflow claim planning.")
        research = [item for item in self.generation.research.list_for_campaign(campaign_id)
                    if item.get("work_item_id") == work_item_id and item.get("status") == "approved"]
        research_ids = [item["package_id"] for item in research]
        evidence = self.generation.extraction.approved_units_for(research_ids)
        if not evidence:
            raise KnowledgeClaimPlanningError("Approved Phase 5 evidence is required before workflow claim planning.")
        package = {
            "package_id": self._stable_id("WKCTX", campaign_id, work_item_id),
            "campaign_id": campaign_id, "gap_id": work.get("gap_id"),
            "work_item_id": work_item_id, "requested_asset_type": "workflow",
            "workflow_identity": str(work.get("target_asset") or work.get("area_id") or work_item_id),
            "workflow_name": str(work.get("target_asset") or work.get("area_id") or "Governed Workflow").replace("_", " ").title(),
            "category": campaign.get("category", "Troubleshooting"),
            "platform": (campaign.get("platforms") or ["Cross-platform"])[0],
            "gap_type": next((gap.get("gap_type") for gap in campaign.get("gaps") or []
                              if gap.get("gap_id") == work.get("gap_id")), None),
            "work_type": work.get("work_type"), "proposed_purpose": work.get("reason") or work.get("title"),
            "existing_assets_considered": [],
            "workflow_required_coverage_roles": self._workflow_required_coverage_roles(
                research_ids
            ),
        }
        return package, evidence

    def _eligible(self, package_id: str):
        try:
            package = self.generation.get(package_id)
        except KnowledgeDraftGenerationError as error:
            raise KnowledgeClaimPlanningError(str(error)) from error
        if package.get("requested_asset_type") != "knowledge_article":
            raise KnowledgeClaimPlanningError("Claim planning currently supports knowledge articles only.")
        if package.get("generation_status") in {"rejected", "superseded", "accepted_into_content_studio"}:
            raise KnowledgeClaimPlanningError("This package lifecycle no longer permits claim planning.")
        if package.get("reused_assets"):
            raise KnowledgeClaimPlanningError("Canonical reuse already satisfies this package.")
        if self.generation.identity.resolve_published(package.get("canonical_identity")):
            raise KnowledgeClaimPlanningError("A canonical published article already satisfies this package.")
        evidence = self.generation.extraction.approved_units_for(package.get("research_package_ids") or [])
        if not evidence:
            raise KnowledgeClaimPlanningError("Approved Phase 5 evidence is required before claim planning.")
        return package, evidence

    def _claims(self, package, evidence, prior):
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for unit in evidence:
            text = str(unit.get("normalized_claim") or "").strip()
            if not text:
                continue
            section = self.EVIDENCE_SECTION.get(unit.get("evidence_type"), "procedure")
            key = (section, self._norm(text))
            grouped.setdefault(key, []).append(unit)
        claims = []
        for (section, _), units in sorted(grouped.items()):
            text = units[0]["normalized_claim"].strip()
            claim_id = self._stable_id("CLM", package["package_id"], section, self._norm(text))
            old = prior.get(claim_id, {})
            evidence_ids = sorted(unit["evidence_id"] for unit in units)
            claims.append({
                "claim_id": claim_id, "claim_type": self.CLAIM_TYPE.get(units[0].get("evidence_type"),
                                                                          "descriptive"),
                "section": section, "normalized_claim": text,
                "evidence_ids": evidence_ids,
                "provenance": [{"evidence_id": unit["evidence_id"], "source_url": unit.get("source_url"),
                                "source_title": unit.get("source_title"), "publisher": unit.get("publisher")}
                               for unit in units],
                "workflow_coverage_roles": self._unique(
                    role for unit in units for role in unit.get("workflow_coverage_roles") or []
                ),
                "support_level": "corroborated" if len(evidence_ids) > 1 else self._support(units[0]),
                "confidence": "high" if len(evidence_ids) > 1 else units[0].get("confidence", "medium"),
                "applicability": self._unique(unit.get("platform_applicability") for unit in units),
                "limitations": [], "review_state": old.get("review_state", "proposed"),
                "reviewer_notes": old.get("reviewer_notes", ""),
                "reviewed_at": old.get("reviewed_at"), "stale": False,
                "source_urls": self._unique(unit.get("source_url") for unit in units),
            })
        return claims

    def _workflow_required_coverage_roles(self, research_ids: list[str]) -> list[str]:
        required: set[str] = set()
        optional_when_present = {"conditional_input", "branch_handling", "escalation"}
        for research_id in research_ids:
            for extraction in self.generation.extraction.list_for_research(research_id):
                if extraction.get("status") in {"needs_refresh", "failed", "rejected", "superseded"}:
                    continue
                if not (extraction.get("retrieval") or {}).get(
                        "workflow_evidence_compression_policy"):
                    continue
                required.update({"entry_setup", "primary_action", "success_verification"})
                for unit in extraction.get("evidence_units") or []:
                    if (unit.get("content_disposition") or {}).get("status") == "suppressed_non_substantive":
                        continue
                    required.update(set(unit.get("workflow_coverage_roles") or []) & optional_when_present)
        return sorted(required)

    def _workflow_procedure_coverage(self, package: dict[str, Any],
                                     claims: list[dict[str, Any]]):
        required = set(package.get("workflow_required_coverage_roles") or [])
        covered = {
            role for claim in claims for role in claim.get("workflow_coverage_roles") or []
        }
        missing = sorted(required - covered)
        labels = {
            "entry_setup": "entry or setup action",
            "primary_action": "primary action or transition",
            "conditional_input": "conditional credential or input step",
            "success_verification": "success verification",
            "branch_handling": "branch handling",
            "escalation": "escalation boundary",
        }
        gaps = [{
            "gap_id": self._stable_id("GAP", package["package_id"], "workflow_coverage", role),
            "section": "verification" if role == "success_verification" else "procedure",
            "required": True,
            "reason": (
                "Missing-workflow evidence does not yet support the required "
                f"{labels.get(role, role.replace('_', ' '))}."
            ),
            "coverage_role": role,
        } for role in missing]
        return {
            "required": sorted(required), "covered": sorted(required & covered),
            "missing": missing, "complete": not missing,
        }, gaps

    def _workflow_specs(self, package, claims):
        """Attach Phase 8's existing node specification to evidence-bound claims."""
        order = {name: index for index, name in enumerate((
            "prerequisites", "platform_applicability", "safety", "procedure", "commands",
            "alternate_outcomes", "escalation", "verification", "expected_result",
        ))}
        claims = sorted(claims, key=lambda item: (order.get(item["section"], 50), item["claim_id"]))
        terminal_indexes = [index for index, claim in enumerate(claims)
                            if claim["section"] in {"verification", "expected_result"}]
        terminal_index = terminal_indexes[-1] if terminal_indexes else None
        node_ids = [self._workflow_node_id(claim, index == terminal_index)
                    for index, claim in enumerate(claims)]
        for index, claim in enumerate(claims):
            terminal = index == terminal_index
            if terminal:
                fields = {"title": "Verified Result", "message": claim["normalized_claim"]}
                node_type = "resolution"
            else:
                fields = {"title": self._workflow_title(claim),
                          "instruction": claim["normalized_claim"]}
                if index + 1 < len(node_ids):
                    fields["next"] = node_ids[index + 1]
                node_type = "instruction"
            spec = {"node_id": node_ids[index], "type": node_type, "operation": "add",
                    "fields": fields}
            if index == 0:
                spec.update({"start_node": node_ids[0], "workflow_name": package["workflow_name"],
                             "category": package["category"], "platform": package["platform"]})
            claim["workflow_spec"] = spec
        return claims

    def _workflow_spec_gaps(self, package, claims):
        """Validate the deterministic subset of Phase 8's existing claim contract."""
        gaps, seen = [], set()
        for claim in claims:
            spec = claim.get("workflow_spec") or {}
            node_id = str(spec.get("node_id") or "").strip()
            valid = (node_id and node_id not in seen
                     and spec.get("type") in {"question", "instruction", "resolution", "transition"}
                     and isinstance(spec.get("fields"), dict))
            if not valid:
                gaps.append({"gap_id": self._stable_id("GAP", package["package_id"], claim["claim_id"], "workflow_spec"),
                             "section": claim["section"], "required": True,
                             "reason": "The evidence-bound claim could not produce a valid Phase 8 workflow specification."})
            seen.add(node_id)
        if claims and not any((claim.get("workflow_spec") or {}).get("type") == "resolution"
                              for claim in claims):
            gaps.append({"gap_id": self._stable_id("GAP", package["package_id"], "terminal_result"),
                         "section": "verification", "required": True,
                         "reason": "Approved verification or expected-result evidence is required for a terminal result."})
        return gaps

    def _workflow_node_id(self, claim, terminal):
        prefix = "r" if terminal else "i"
        return f"{prefix}_{claim['claim_id'].split('-', 1)[-1].casefold()}"

    @staticmethod
    def _workflow_title(claim):
        labels = {"caution": "Review Safety Requirement", "authorization_requirement": "Confirm Authorization",
                  "verification": "Verify the Result", "expected_result": "Observe the Expected Result",
                  "command": "Run the Supported Command", "escalation": "Escalate with Evidence",
                  "prerequisite": "Confirm the Prerequisite", "applicability": "Confirm Applicability"}
        return labels.get(claim.get("claim_type"), "Perform the Supported Action")

    def _sections(self, package, claims, conflicts, reuse, prior=None):
        prior = prior or {}
        by_section = {name: [claim for claim in claims if claim["section"] == name]
                      for name in self.SECTION_ORDER}
        # Campaign planning is identity context, not a technical claim.
        required_sections = self._required_sections(package)
        purpose_supported = bool(package.get("proposed_purpose"))
        source_supported = bool(claims)
        sections, gaps = [], []
        for name in self.SECTION_ORDER:
            section_claims = by_section[name]
            applicable = name in required_sections or bool(section_claims) or name in {
                "symptoms", "platform_applicability"
            }
            supported = bool(section_claims) or (name == "purpose" and purpose_supported) or (
                name == "sources" and source_supported)
            section_conflicts = [item["conflict_id"] for item in conflicts if item["section"] == name]
            section_reuse = [item for item in reuse if name in item.get("sections", [])]
            missing = applicable and name in required_sections and not supported
            if missing:
                gap_id = self._stable_id("GAP", package["package_id"], name)
                gaps.append({"gap_id": gap_id, "section": name, "required": True,
                             "reason": f"Required section '{name}' lacks approved supporting evidence."})
            old = prior.get(name, {})
            deterministic_state = ("supported_context" if supported and not section_claims else
                                   "not_applicable" if not applicable else
                                   "needs_evidence" if missing else "proposed")
            preserve_human_review = (
                old.get("review_state") in SECTION_REVIEW_STATES
                and bool(old.get("reviewed_at"))
            )
            sections.append({"section": name, "applicable": applicable,
                             "required": name in required_sections,
                             "claim_ids": [item["claim_id"] for item in section_claims],
                             "evidence_ids": self._unique(eid for item in section_claims for eid in item["evidence_ids"]),
                             "missing_evidence": missing, "conflict_ids": section_conflicts,
                             "canonical_reuse": section_reuse,
                             "review_state": (old.get("review_state")
                                              if preserve_human_review else deterministic_state),
                             "reviewer_notes": (old.get("reviewer_notes", "")
                                                if preserve_human_review else ""),
                             "reviewed_at": (old.get("reviewed_at")
                                             if preserve_human_review else None)})
        return sections, gaps

    def _required_sections(self, package):
        if package.get("requested_asset_type") != "workflow":
            return self.REQUIRED
        required = {"procedure", "verification", "sources"}
        if package.get("work_type") == "safety_review" or package.get("gap_type") == "missing_safety":
            required.add("safety")
        return required

    @staticmethod
    def _extraction_ids(evidence):
        return sorted({(unit.get("provenance") or {}).get("extraction_id")
                       for unit in evidence if (unit.get("provenance") or {}).get("extraction_id")})

    def _conflicts(self, claims):
        conflicts = []
        for section in {item["section"] for item in claims}:
            candidates = [item for item in claims if item["section"] == section]
            for left_index, left in enumerate(candidates):
                for right in candidates[left_index + 1:]:
                    reason = self._conflict_reason(left, right)
                    if not reason:
                        continue
                    evidence_ids = sorted(set(left["evidence_ids"] + right["evidence_ids"]))
                    conflicts.append({"conflict_id": self._stable_id("CNF", section, *evidence_ids),
                                      "section": section, "claim_ids": [left["claim_id"], right["claim_id"]],
                                      "evidence_ids": evidence_ids, "reason": reason,
                                      "resolution": None, "reviewer_notes": "", "reviewed_at": None})
        return conflicts

    @staticmethod
    def _conflict_reason(left, right):
        ltext, rtext = left["normalized_claim"].casefold(), right["normalized_claim"].casefold()
        opposites = (("must ", "must not "), ("requires ", "does not require "),
                     ("supported", "not supported"), ("enable", "disable"))
        if any((a in ltext and b in rtext) or (b in ltext and a in rtext) for a, b in opposites):
            return "Approved evidence contains potentially contradictory requirements or outcomes."
        lapp, rapp = set(left.get("applicability") or []), set(right.get("applicability") or [])
        if lapp and rapp and lapp.isdisjoint(rapp) and left["claim_type"] == right["claim_type"]:
            return "Claim applicability differs across approved evidence and requires human scoping."
        return None

    def _canonical_reuse(self, package):
        values = []
        for asset in package.get("existing_assets_considered") or []:
            if asset.get("content_type") != "article" or asset.get("state") != "published":
                continue
            match = self.generation.identity.resolve_published(asset.get("identifier"))
            if match and match.article.get("review", {}).get("status") == "approved":
                values.append({"article_id": match.article["id"], "title": match.article.get("title"),
                               "sections": ["related_knowledge"], "decision": "candidate_reuse",
                               "traceable": True})
        return values

    @staticmethod
    def _support(unit):
        text = str(unit.get("normalized_claim") or "").casefold()
        if any(term in text for term in ("may ", "might ", "can sometimes", "if ", "when ")):
            return "conditional"
        if unit.get("confidence") == "low":
            return "partial"
        return "direct"

    @staticmethod
    def _status(claims, conflicts, gaps, sections=None):
        if any(item.get("resolution") in {None, "", "deferred"} for item in conflicts):
            return "needs_conflict_resolution"
        if any(item.get("required") for item in gaps):
            return "needs_evidence"
        states = [item.get("review_state") for item in claims]
        mapped_sections = [item for item in (sections or []) if item.get("claim_ids")]
        sections_approved = all(item.get("review_state") == "approved" for item in mapped_sections)
        if states and all(state == "approved" for state in states) and sections_approved:
            return "ready_for_drafting"
        if any(state == "approved" for state in states):
            return "partially_approved"
        return "needs_review"

    def _path(self, plan_id):
        if not re.fullmatch(r"KCPM-[A-F0-9]{12}", str(plan_id or "")):
            raise KnowledgeClaimPlanningError("Invalid claim-plan ID.")
        return self.package_root / f"{plan_id}.json"

    def _save(self, plan):
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(plan["claim_plan_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeClaimPlanningError(f"Unable to read claim plan: {error}") from error

    @staticmethod
    def _event(plan, event, at, **details):
        record = {"event": event, "at": at, **details}
        comparable = {key: value for key, value in record.items() if key != "at"}
        if plan.get("history") and {key: value for key, value in plan["history"][-1].items()
                                    if key != "at"} == comparable:
            return
        plan.setdefault("history", []).append(record)

    @staticmethod
    def _stable_id(prefix, *parts):
        digest = hashlib.sha256("|".join(str(item) for item in parts).encode()).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _norm(value):
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value).casefold())).strip()

    @staticmethod
    def _unique(values):
        return list(dict.fromkeys(value for value in values if value))
