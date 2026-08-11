from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator
from app.services.knowledge_claim_planning_service import (
    KnowledgeClaimPlanningError,
    KnowledgeClaimPlanningService,
)
from app.services.knowledge_draft_generation_service import (
    KnowledgeDraftGenerationError,
    KnowledgeDraftGenerationService,
)


ASSEMBLY_STATUSES = (
    "proposed", "assembling", "assembled", "needs_revision", "needs_evidence",
    "stale", "ready_for_review", "rejected", "superseded", "handed_off",
)


class KnowledgeDraftAssemblyError(ValueError):
    pass


class KnowledgeDraftAssemblyService:
    """Deterministically assembles a reviewed claim plan into a governed draft."""

    SECTION_ORDER = KnowledgeClaimPlanningService.SECTION_ORDER
    STATE_CHANGING = re.compile(
        r"\b(restart|install|uninstall|delete|remove|reset|modify|change|enable|disable|"
        r"update|rollback|format|partition|flash)\b", re.IGNORECASE
    )

    def __init__(self, generation: KnowledgeDraftGenerationService | None = None,
                 campaign_root: Path | None = None):
        self.generation = generation or KnowledgeDraftGenerationService()
        self.campaign_root = (campaign_root or self.generation.campaign_root).resolve()
        self.package_root = self.campaign_root / "draft_assembly"
        self.planning = KnowledgeClaimPlanningService(self.generation, self.campaign_root)

    def list_for_kdg(self, package_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        values = [self._read(path) for path in self.package_root.glob("KASM-*.json")]
        return sorted((self._with_current_state(value) for value in values
                       if value.get("kdg_package_id") == package_id),
                      key=lambda value: value.get("created_at", ""), reverse=True)

    def get(self, assembly_id: str) -> dict[str, Any]:
        path = self._path(assembly_id)
        if not path.exists():
            raise KnowledgeDraftAssemblyError(f"Assembly '{assembly_id}' was not found.")
        return self._with_current_state(self._read(path))

    def is_eligible(self, plan_id: str) -> bool:
        try:
            self._eligible(plan_id)
            return True
        except KnowledgeDraftAssemblyError:
            return False

    def assemble(self, plan_id: str) -> dict[str, Any]:
        package, plan, claims = self._eligible(plan_id)
        assembly_id = self._stable_id("KASM", package["package_id"], plan_id)
        path = self._path(assembly_id)
        prior = self._read(path) if path.exists() else None
        fingerprint = self._fingerprint({
            "plan_fingerprint": plan.get("input_fingerprint"),
            "claims": [{key: claim.get(key) for key in (
                "claim_id", "section", "claim_type", "normalized_claim", "evidence_ids",
                "support_level", "applicability", "limitations", "review_state"
            )} for claim in claims],
            "sections": plan.get("sections"), "reuse": plan.get("canonical_reuse"),
        })
        if prior and prior.get("fingerprint") == fingerprint:
            return self._with_current_state(prior)

        now = self.generation._now()
        revisions = deepcopy((prior or {}).get("revisions") or [])
        history = deepcopy((prior or {}).get("history") or [])
        if prior and prior.get("fingerprint"):
            revisions.append({
                "fingerprint": prior["fingerprint"], "status": prior.get("status"),
                "assembled_content": deepcopy(prior.get("assembled_content")),
                "section_map": deepcopy(prior.get("section_map")), "superseded_at": now,
            })
            history.append({"event": "prior_assembly_superseded", "at": now,
                            "actor": "Deterministic Draft Assembler"})

        section_map = self._section_map(plan, claims)
        sources = self._sources(claims)
        reuse = [deepcopy(item) for item in plan.get("canonical_reuse") or []
                 if item.get("decision") in {"approved_reuse", "reuse", "approved"}]
        article = self._article(package, plan, claims, section_map, sources, reuse, assembly_id)
        validation = self._validate(package, plan, claims, section_map, sources, article)
        blockers = [item for item in validation if item["level"] == "error"]
        warnings = [item["message"] for item in validation if item["level"] == "warning"]
        applicable = [item for item in section_map if item["applicable"]]
        supported = [item for item in applicable if item["claim_ids"] or item["section"] in {"purpose", "sources"}]
        status = "needs_evidence" if any("evidence" in item["message"].casefold() for item in blockers) else (
            "needs_revision" if blockers else "ready_for_review"
        )
        record = {
            "schema_version": "1.0", "assembly_id": assembly_id,
            "kdg_package_id": package["package_id"], "claim_plan_id": plan_id,
            "campaign_id": package["campaign_id"], "gap_id": package["gap_id"],
            "work_item_id": package["work_item_id"], "article_identity": package["canonical_identity"],
            "article_title": package["proposed_title"], "approved_claim_ids": [c["claim_id"] for c in claims],
            "excluded_claim_ids": [c["claim_id"] for c in plan.get("claims") or []
                                   if c.get("review_state") != "approved" or c.get("stale")],
            "supporting_evidence_ids": self._unique(e for c in claims for e in c.get("evidence_ids") or []),
            "canonical_reuse": reuse, "source_provenance": sources, "section_map": section_map,
            "assembled_content": article, "validation_results": validation,
            "completeness": round(100 * len(supported) / len(applicable)) if applicable else 0,
            "warnings": warnings, "status": status, "fingerprint": fingerprint,
            "content_studio_article_id": (prior or {}).get("content_studio_article_id"),
            "created_at": (prior or {}).get("created_at", now), "updated_at": now,
            "history": history + [{"event": "draft_assembled", "at": now,
                                    "actor": "Deterministic Draft Assembler",
                                    "status": status}], "revisions": revisions,
        }
        self._save(record)
        package["assembly_id"] = assembly_id
        package["assembly_fingerprint"] = fingerprint
        package["draft_preview"] = deepcopy(article)
        package["validation_results"] = deepcopy(validation)
        package["generation_status"] = "ready_for_review" if status == "ready_for_review" else status
        package["updated_at"] = now
        package.setdefault("history", []).append({"event": "supervised_draft_assembled", "at": now,
                                                   "actor": "Human-initiated Assembly",
                                                   "assembly_id": assembly_id})
        self.generation._save(package)
        return deepcopy(record)

    def handoff(self, assembly_id: str) -> dict[str, Any]:
        record = self.get(assembly_id)
        if record.get("status") == "handed_off":
            return record
        if record.get("status") != "ready_for_review":
            raise KnowledgeDraftAssemblyError("Only a current, validated assembly can enter Content Studio.")
        package = self.generation.get(record["kdg_package_id"])
        package["draft_preview"] = deepcopy(record["assembled_content"])
        package["generation_status"] = "ready_for_review"
        self.generation._save(package)
        accepted = self.generation.accept_into_content_studio(package["package_id"])
        if accepted.get("generation_status") != "accepted_into_content_studio":
            raise KnowledgeDraftAssemblyError("Canonical identity governance blocked the Content Studio handoff.")
        stored = self._read(self._path(assembly_id))
        stored["status"] = "handed_off"
        stored["content_studio_article_id"] = accepted["content_studio_article_id"]
        stored["updated_at"] = self.generation._now()
        stored.setdefault("history", []).append({"event": "sent_to_content_studio",
                                                 "at": stored["updated_at"], "actor": "Human"})
        self._save(stored)
        return deepcopy(stored)

    def _eligible(self, plan_id):
        try:
            plan = self.planning.get(plan_id)
            package = self.generation.get(plan["kdg_package_id"])
        except (KnowledgeClaimPlanningError, KnowledgeDraftGenerationError, KeyError) as error:
            raise KnowledgeDraftAssemblyError(str(error)) from error
        if package.get("requested_asset_type") != "knowledge_article":
            raise KnowledgeDraftAssemblyError("Assembly currently supports knowledge articles only.")
        if package.get("generation_status") in {"rejected", "superseded", "accepted_into_content_studio"}:
            raise KnowledgeDraftAssemblyError("This package lifecycle no longer permits assembly.")
        if plan.get("status") != "ready_for_drafting":
            raise KnowledgeDraftAssemblyError("A current ready-for-drafting claim plan is required.")
        claims = [item for item in plan.get("claims") or []
                  if item.get("review_state") == "approved" and not item.get("stale")]
        if not claims or len(claims) != len(plan.get("claims") or []):
            raise KnowledgeDraftAssemblyError("Every planned claim must be approved and current.")
        if any(item.get("resolution") in {None, "", "deferred"} for item in plan.get("conflicts") or []):
            raise KnowledgeDraftAssemblyError("Resolve every required evidence conflict before assembly.")
        if any(item.get("required") for item in plan.get("evidence_gaps") or []):
            raise KnowledgeDraftAssemblyError("Close required evidence gaps before assembly.")
        if self.generation.identity.resolve_published(package.get("canonical_identity")):
            raise KnowledgeDraftAssemblyError("A canonical published article already satisfies this identity.")
        return package, plan, claims

    def _with_current_state(self, record):
        value = deepcopy(record)
        if value.get("status") in {"handed_off", "rejected", "superseded"}:
            return value
        try:
            plan = self.planning.get(value["claim_plan_id"])
        except (KnowledgeClaimPlanningError, KeyError):
            value["status"] = "stale"
            return value
        current = self._fingerprint({
            "plan_fingerprint": plan.get("input_fingerprint"),
            "claims": [{key: claim.get(key) for key in (
                "claim_id", "section", "claim_type", "normalized_claim", "evidence_ids",
                "support_level", "applicability", "limitations", "review_state"
            )} for claim in plan.get("claims") or [] if claim.get("review_state") == "approved"],
            "sections": plan.get("sections"), "reuse": plan.get("canonical_reuse"),
        })
        if plan.get("status") != "ready_for_drafting" or current != value.get("fingerprint"):
            value["status"] = "stale"
            value.setdefault("warnings", []).append("The approved claim plan changed; explicit reassembly is required.")
        return value

    def _section_map(self, plan, claims):
        by_id = {item["claim_id"]: item for item in claims}
        values = []
        for planned in plan.get("sections") or []:
            if not planned.get("applicable"):
                continue
            ordered = [by_id[claim_id] for claim_id in planned.get("claim_ids") or [] if claim_id in by_id]
            values.append({"section": planned["section"], "applicable": True,
                           "required": planned.get("required", False),
                           "claim_ids": [item["claim_id"] for item in ordered],
                           "evidence_ids": self._unique(e for item in ordered for e in item.get("evidence_ids") or []),
                           "content": [item["normalized_claim"] for item in ordered],
                           "support_levels": [item.get("support_level") for item in ordered]})
        return values

    def _article(self, package, plan, claims, sections, sources, reuse, assembly_id):
        article = create_article_template()
        content = {item["section"]: item["content"] for item in sections}
        campaign = self.generation.planner.get(package["campaign_id"])
        command_claims = [item for item in claims if item.get("section") == "commands"]
        commands = [{"command": item["normalized_claim"], "description": item["normalized_claim"]}
                    for item in command_claims]
        purpose = content.get("purpose") or [package.get("proposed_purpose")]
        article.update({
            "id": package["canonical_identity"], "canonical_id": package["canonical_identity"],
            "title": package["proposed_title"], "category": campaign.get("category") or "Knowledge",
            "difficulty": "Beginner", "estimated_time": "Review required",
            "overview": " ".join(item for item in purpose if item),
            "tags": sorted({str(package.get("platform") or "").casefold(),
                            str(package.get("coverage_area") or "").replace("_", " ").casefold()} - {""}),
            "checklist": content.get("procedure") or [],
            "common_indicators": content.get("symptoms") or content.get("expected_result") or [],
            "commands": commands,
            "related_topics": self._unique(
                list(content.get("related_knowledge") or []) + [item["article_id"] for item in reuse]
            ),
            "quiz": [], "sources": [{"title": item["source_title"], "url": item["source_url"]}
                                      for item in sources],
            "generation": {"provider": "Gnojo Knowledge Factory",
                           "model": "deterministic-plan-assembler-v1",
                           "generated_at": self.generation._now()},
            "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None,
                       "notes": ["Assembled from human-approved claims; publication review is required."]},
            "assembly_sections": {name: content.get(name, []) for name in self.SECTION_ORDER
                                  if content.get(name)},
            "knowledge_factory": {
                "generation_package_id": package["package_id"], "campaign_id": package["campaign_id"],
                "gap_id": package["gap_id"], "work_item_id": package["work_item_id"],
                "research_package_ids": list(package.get("research_package_ids") or []),
                "claim_plan_id": plan["claim_plan_id"], "assembly_id": assembly_id,
                "claim_ids": [item["claim_id"] for item in claims],
                "evidence_ids": self._unique(e for item in claims for e in item.get("evidence_ids") or []),
                "section_claim_map": {item["section"]: item["claim_ids"] for item in sections},
                "canonical_reuse_ids": [item["article_id"] for item in reuse],
            },
        })
        return article

    def _sources(self, claims):
        values, seen = [], set()
        for claim in claims:
            for item in claim.get("provenance") or []:
                url = str(item.get("source_url") or "").strip()
                title = str(item.get("source_title") or "").strip()
                if not url or not title or url in seen:
                    continue
                seen.add(url)
                values.append({"source_title": title, "source_url": url,
                               "publisher": item.get("publisher"),
                               "claim_ids": [claim["claim_id"]],
                               "evidence_ids": [item.get("evidence_id")]})
        # A repeated URL supports multiple claims but remains one article source.
        for value in values:
            for claim in claims:
                if any(item.get("source_url") == value["source_url"]
                       for item in claim.get("provenance") or []):
                    value["claim_ids"] = self._unique(value["claim_ids"] + [claim["claim_id"]])
                    value["evidence_ids"] = self._unique(value["evidence_ids"] + claim.get("evidence_ids", []))
        return values

    def _validate(self, package, plan, claims, sections, sources, article):
        results = [{"level": "error", "check": "article_schema", "message": error}
                   for error in ArticleValidator.validate(article)]
        current_ids = set(plan.get("approved_evidence_ids") or [])
        used_ids = {item for claim in claims for item in claim.get("evidence_ids") or []}
        checks = [
            (all(item.get("review_state") == "approved" and not item.get("stale") for item in claims),
             "claim_approval", "Every assembled claim is human approved and current."),
            (used_ids.issubset(current_ids), "evidence_freshness", "Every supporting EVD is current."),
            (all(item.get("evidence_ids") for item in claims), "provenance", "Every claim retains EVD provenance."),
            (bool(sources), "source_attribution", "Assembled claims retain supporting sources."),
            (not self.generation.identity.resolve_published(package["canonical_identity"]),
             "canonical_identity", "No published canonical identity conflict exists."),
            (not self.generation.identity.resolve_published(package["canonical_identity"]),
             "duplicate_check", "No published duplicate of the canonical article exists."),
            (all(not item.get("required") or item.get("content") or item["section"] in {"purpose", "sources"}
                 for item in sections),
             "required_sections", "Every required applicable section is supported."),
            (all(not item.get("content") or item.get("claim_ids") or item["section"] in {"purpose", "sources"}
                 for item in sections),
             "unsupported_guidance", "Every assembled technical section is backed by approved claims."),
        ]
        for passed, check, message in checks:
            results.append({"level": "passed" if passed else "error", "check": check,
                            "message": message if passed else f"Validation failed: {message}"})
        content = {item["section"]: item["content"] for item in sections}
        verification_required = any(item.get("section") == "verification" and item.get("required")
                                    for item in plan.get("sections") or [])
        if verification_required and not content.get("verification"):
            results.append({"level": "error", "check": "verification",
                            "message": "Required verification guidance is missing."})
        else:
            results.append({"level": "passed", "check": "verification",
                            "message": "Verification requirements are satisfied."})
        state_changing = any(self.STATE_CHANGING.search(item["normalized_claim"])
                             for item in claims if item.get("section") in {"procedure", "commands"})
        safety = bool(content.get("safety"))
        if state_changing and not safety:
            results.append({"level": "error", "check": "safety_authorization",
                            "message": "State-changing procedures require approved safety or authorization guidance."})
        else:
            results.append({"level": "passed", "check": "safety_authorization",
                            "message": "Safety and authorization requirements are preserved."})
        return results or [{"level": "passed", "check": "assembly", "message": "Assembly validation passed."}]

    def _path(self, assembly_id):
        if not re.fullmatch(r"KASM-[A-F0-9]{12}", str(assembly_id or "")):
            raise KnowledgeDraftAssemblyError("Invalid assembly ID.")
        return self.package_root / f"{assembly_id}.json"

    def _save(self, value):
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(value["assembly_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeDraftAssemblyError(f"Unable to read assembly: {error}") from error

    @staticmethod
    def _stable_id(prefix, *parts):
        digest = hashlib.sha256("|".join(str(item) for item in parts).encode()).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _unique(values):
        return list(dict.fromkeys(item for item in values if item))
