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

    def plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.get(plan_id)
        package, evidence = self._eligible(plan["kdg_package_id"])
        fingerprint = self._fingerprint([
            {key: unit.get(key) for key in ("evidence_id", "evidence_type", "normalized_claim",
                                             "fingerprint", "review_state")}
            for unit in sorted(evidence, key=lambda value: value["evidence_id"])
        ])
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
                           "required_gaps": len([item for item in gaps if item["required"]])},
        })
        self._event(plan, "claim_plan_built", now, actor="Deterministic Claim Planner",
                    claim_count=len(claims), conflict_count=len(conflicts), gap_count=len(gaps))
        self._save(plan)
        return self.get(plan_id)

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

    def _with_current_evidence_state(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Decorate a stored plan with live Phase 5 evidence state without mutating it."""
        value = deepcopy(plan)
        try:
            package = self.generation.get(value["kdg_package_id"])
            current_ids = {unit["evidence_id"] for unit in self.generation.extraction.approved_units_for(
                package.get("research_package_ids") or []
            )}
        except (KnowledgeDraftGenerationError, KeyError):
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
        return value

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
                "support_level": "corroborated" if len(evidence_ids) > 1 else self._support(units[0]),
                "confidence": "high" if len(evidence_ids) > 1 else units[0].get("confidence", "medium"),
                "applicability": self._unique(unit.get("platform_applicability") for unit in units),
                "limitations": [], "review_state": old.get("review_state", "proposed"),
                "reviewer_notes": old.get("reviewer_notes", ""),
                "reviewed_at": old.get("reviewed_at"), "stale": False,
            })
        return claims

    def _sections(self, package, claims, conflicts, reuse, prior=None):
        prior = prior or {}
        by_section = {name: [claim for claim in claims if claim["section"] == name]
                      for name in self.SECTION_ORDER}
        # Campaign planning is identity context, not a technical claim.
        purpose_supported = bool(package.get("proposed_purpose"))
        source_supported = bool(claims)
        sections, gaps = [], []
        for name in self.SECTION_ORDER:
            section_claims = by_section[name]
            applicable = name in self.REQUIRED or bool(section_claims) or name in {
                "symptoms", "platform_applicability"
            }
            supported = bool(section_claims) or (name == "purpose" and purpose_supported) or (
                name == "sources" and source_supported)
            section_conflicts = [item["conflict_id"] for item in conflicts if item["section"] == name]
            section_reuse = [item for item in reuse if name in item.get("sections", [])]
            missing = applicable and name in self.REQUIRED and not supported
            if missing:
                gap_id = self._stable_id("GAP", package["package_id"], name)
                gaps.append({"gap_id": gap_id, "section": name, "required": True,
                             "reason": f"Required section '{name}' lacks approved supporting evidence."})
            old = prior.get(name, {})
            deterministic_state = ("supported_context" if supported and not section_claims else
                                   "not_applicable" if not applicable else
                                   "needs_evidence" if missing else "proposed")
            sections.append({"section": name, "applicable": applicable,
                             "required": name in self.REQUIRED,
                             "claim_ids": [item["claim_id"] for item in section_claims],
                             "evidence_ids": self._unique(eid for item in section_claims for eid in item["evidence_ids"]),
                             "missing_evidence": missing, "conflict_ids": section_conflicts,
                             "canonical_reuse": section_reuse,
                             "review_state": old.get("review_state", deterministic_state),
                             "reviewer_notes": old.get("reviewer_notes", ""),
                             "reviewed_at": old.get("reviewed_at")})
        return sections, gaps

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
