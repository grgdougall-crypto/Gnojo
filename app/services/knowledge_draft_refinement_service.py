from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from app.knowledge.article_validator import ArticleValidator
from app.services.knowledge_draft_generation_service import (
    KnowledgeDraftGenerationService,
)


class KnowledgeDraftRefinementError(ValueError):
    pass


class KnowledgeDraftRefinementService:
    """Human-initiated, deterministic refinement inside an existing KDG package."""

    SECTION_STATUSES = (
        "supported", "partial", "missing", "not_applicable",
        "needs_evidence", "needs_human_review",
    )
    REQUIRED_SECTIONS = {"purpose", "procedure", "verification", "sources"}
    SECTION_ORDER = (
        "purpose", "symptoms", "preconditions", "safety", "procedure",
        "verification", "expected_result", "alternate_outcomes", "escalation",
        "related_articles", "commands", "sources", "platform_applicability",
    )

    def __init__(self, generation: KnowledgeDraftGenerationService | None = None):
        self.generation = generation or KnowledgeDraftGenerationService()
        self.repository = self.generation.repository
        self.identity = self.generation.identity

    def refine(self, package_id: str) -> dict[str, Any]:
        package = self.generation.get(package_id)
        self._assert_eligible(package)
        evidence, reusable = self._authorized_evidence(package)
        fingerprint = self._fingerprint({
            "preview": package.get("draft_preview"), "evidence": evidence,
            "reusable": reusable, "identity": package.get("canonical_identity"),
        })
        current = package.get("refinement") or {}
        if current.get("input_fingerprint") == fingerprint and current.get("status") in {
                "ready_for_review", "needs_evidence", "needs_revision"}:
            return package

        now = self.generation._now()
        package["generation_status"] = "refinement_in_progress"
        self._event(package, "refinement_started", now, fingerprint=fingerprint)
        sections, claims = self._section_coverage(package, evidence)
        unsupported = self._unsupported_guidance(package["draft_preview"], claims)
        refined = self._compose(package, sections, claims, evidence, reusable)
        result_fingerprint = self._fingerprint({
            "preview": refined, "evidence": evidence,
            "reusable": reusable, "identity": package.get("canonical_identity"),
        })
        schema_errors = ArticleValidator.validate(refined)
        identity_match = self.identity.resolve(
            package["canonical_identity"], refined, include_drafts=True,
        )
        duplicate = identity_match
        required_incomplete = [item["section"] for item in sections
                               if item["required"] and item["status"] not in {"supported", "not_applicable"}]
        material_unsupported = [item for item in unsupported if item["material"]]
        if duplicate:
            status = "needs_revision"
            warnings = [f"Canonical reuse is required: existing article '{duplicate.article['id']}' matches this draft."]
        elif schema_errors or material_unsupported:
            status = "needs_revision"
            warnings = [*schema_errors, *[item["reason"] for item in material_unsupported]]
        elif required_incomplete:
            status = "needs_evidence"
            warnings = [f"Required section needs authorized evidence: {name.replace('_', ' ')}."
                        for name in required_incomplete]
        else:
            status = "ready_for_review"
            warnings = ["Deterministic refinement completed; human technical and editorial review remains required."]

        refinement = {
            "schema_version": "1.0", "status": status, "method": "deterministic-evidence-refiner-v1",
            "input_fingerprint": result_fingerprint, "evidence_records": evidence,
            "reusable_knowledge": reusable, "section_coverage": sections,
            "claim_traceability": claims, "unsupported_guidance": unsupported,
            "completeness_percent": self._completeness(sections),
            "validation": {
                "article_schema_errors": schema_errors,
                "provenance_valid": bool(evidence) and all(item.get("approved") for item in evidence),
                "identity_conflict": duplicate.article.get("id") if duplicate else None,
                "required_incomplete": required_incomplete,
                "material_unsupported_count": len(material_unsupported),
            },
            "started_at": current.get("started_at") or now, "updated_at": now,
        }
        package["refinement"] = refinement
        package["draft_preview"] = refined
        package["generation_status"] = status
        package["validation_results"] = self._validation_results(refinement)
        package["warnings"] = warnings
        package["confidence"] = "high" if status == "ready_for_review" else "low"
        package["updated_at"] = now
        self._event(package, "evidence_evaluated", now, evidence_count=len(evidence))
        self._event(package, "refinement_completed", now, status=status,
                    completeness_percent=refinement["completeness_percent"])
        self.generation._save(package)
        self.generation._sync_reference(package)
        return deepcopy(package)

    def _assert_eligible(self, package: dict[str, Any]) -> None:
        if package.get("requested_asset_type") != "knowledge_article":
            raise KnowledgeDraftRefinementError("Phase 4 currently refines knowledge-article packages only.")
        if not package.get("draft_preview"):
            raise KnowledgeDraftRefinementError("Prepare an evidence-backed Phase 3 draft before refinement.")
        if package.get("generation_status") not in {"ready_for_review", "needs_revision", "needs_evidence"}:
            raise KnowledgeDraftRefinementError(
                "Only an active prepared draft may be refined; accepted, rejected, or superseded packages are ineligible."
            )

    def _authorized_evidence(self, package: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        reusable: list[dict[str, Any]] = []
        approved_urls = {item.get("url") for item in package.get("approved_sources_used") or []}
        from app.services.knowledge_claim_planning_service import KnowledgeClaimPlanningService
        planned_claims = KnowledgeClaimPlanningService(
            self.generation, self.generation.campaign_root
        ).approved_claims_for(package["package_id"])
        for claim in planned_claims:
            records.append({
                "evidence_id": claim["claim_id"], "kind": "approved_claim_plan",
                "approved": True, "claim_plan_id": package.get("claim_plan_id"),
                "title": "Human-approved evidence-to-claim plan",
                "claims": [{"section": claim.get("section") or "procedure",
                            "text": claim.get("normalized_claim", "")}],
                "supporting_evidence_ids": list(claim.get("evidence_ids") or []),
                "provenance": deepcopy(claim.get("provenance") or []),
            })
        for unit in self.generation.extraction.approved_units_for(
                package.get("research_package_ids") or []):
            records.append({
                "evidence_id": unit["evidence_id"], "kind": "approved_extracted_evidence",
                "approved": True,
                "research_package_id": (unit.get("provenance") or {}).get("research_package_id"),
                "source_candidate_id": (unit.get("provenance") or {}).get("source_candidate_id"),
                "title": unit.get("source_title"), "url": unit.get("source_url"),
                "content_digest": unit.get("fingerprint"),
                "claims": [{"section": unit.get("evidence_type") or "procedure",
                            "text": unit.get("normalized_claim", "")}],
                "provenance": deepcopy(unit.get("provenance") or {}),
            })
        for package_id in package.get("research_package_ids") or []:
            try:
                research = self.generation.research.get(package_id)
            except Exception:
                continue
            if research.get("status") != "approved":
                continue
            selected = set(research.get("selected_sources") or [])
            for candidate in research.get("candidate_sources") or []:
                cid = candidate.get("source_candidate_id")
                url = candidate.get("canonical_url")
                if cid not in selected or candidate.get("review_state") != "selected" or url not in approved_urls:
                    continue
                source_id = self._stable_id("EVD", package_id, cid)
                records.append({
                    "evidence_id": source_id, "kind": "approved_source", "approved": True,
                    "research_package_id": package_id, "source_candidate_id": cid,
                    "title": candidate.get("page_title") or "Authoritative source", "url": url,
                    "content_digest": (candidate.get("provenance") or {}).get("content_digest"),
                    "claims": self._approved_claims(candidate),
                })
        for asset in package.get("existing_assets_considered") or []:
            if asset.get("content_type") != "article" or asset.get("state") != "published":
                continue
            match = self.identity.resolve_published(asset.get("identifier"))
            if not match or match.article.get("review", {}).get("status") != "approved":
                continue
            article = match.article
            evidence_id = self._stable_id("EVD", "gnojo", article["id"])
            claims = self._article_claims(article)
            records.append({"evidence_id": evidence_id, "kind": "canonical_gnojo_article",
                            "approved": True, "article_id": article["id"],
                            "title": article.get("title"), "claims": claims})
            reusable.append({"article_id": article["id"], "title": article.get("title"),
                             "match_method": "campaign_existing_asset", "evidence_id": evidence_id})
        planning_id = self._stable_id("EVD", "planning", package["package_id"])
        records.append({"evidence_id": planning_id, "kind": "campaign_planning", "approved": True,
                        "title": "Approved campaign planning context", "claims": [
                            {"section": "purpose", "text": package["proposed_purpose"]},
                            {"section": "platform_applicability", "text": package["platform"]},
                        ]})
        return self._deduplicate_records(records), reusable

    @staticmethod
    def _approved_claims(candidate: dict[str, Any]) -> list[dict[str, str]]:
        claims = []
        for value in candidate.get("approved_evidence") or []:
            if isinstance(value, str):
                section, text = "procedure", value.strip()
            elif isinstance(value, dict):
                section, text = str(value.get("section") or "procedure"), str(value.get("text") or "").strip()
            else:
                continue
            if text:
                claims.append({"section": section, "text": text})
        return claims

    @staticmethod
    def _article_claims(article: dict[str, Any]) -> list[dict[str, str]]:
        values = [("purpose", article.get("overview"))]
        values += [("symptoms", item) for item in article.get("common_indicators") or []]
        values += [("procedure", item) for item in article.get("checklist") or []]
        values += [("commands", f"{item.get('command')}: {item.get('description')}")
                   for item in article.get("commands") or [] if isinstance(item, dict)]
        return [{"section": section, "text": str(text).strip()} for section, text in values if str(text or "").strip()]

    def _section_coverage(self, package: dict[str, Any], evidence: list[dict[str, Any]]):
        by_section: dict[str, list[tuple[str, str]]] = {}
        for record in evidence:
            for claim in record.get("claims") or []:
                section = claim.get("section") if claim.get("section") in self.SECTION_ORDER else "procedure"
                by_section.setdefault(section, []).append((record["evidence_id"], claim["text"]))
        # Planning metadata supports identity/purpose and applicability, but never technical procedure.
        source_ids = [item["evidence_id"] for item in evidence if item["kind"] == "approved_source"]
        if source_ids:
            by_section["sources"] = [(item, "Approved source reference") for item in source_ids]

        sections, traceability = [], []
        for name in self.SECTION_ORDER:
            required = name in self.REQUIRED_SECTIONS
            claims = by_section.get(name, [])
            applicable = required or bool(claims) or name in {"symptoms", "platform_applicability"}
            if not applicable:
                status = "not_applicable"
                reason = "No deterministic evidence makes this optional section applicable."
            elif claims:
                status = "supported"
                reason = "Every included claim is linked to authorized evidence."
            elif required:
                status = "needs_evidence"
                reason = "This required section has no authorized claim evidence."
            else:
                status = "missing"
                reason = "No authorized evidence currently supports this useful optional section."
            claim_ids = []
            for evidence_id, text in claims:
                claim_id = self._stable_id("CLM", package["package_id"], name, text)
                claim_ids.append(claim_id)
                traceability.append({"claim_id": claim_id, "section": name, "text": text,
                                     "evidence_ids": [evidence_id], "supported": True})
            sections.append({"section": name, "required": required, "status": status,
                             "reason": reason, "claim_ids": claim_ids})
        return sections, traceability

    @staticmethod
    def _unsupported_guidance(article: dict[str, Any], claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        supported = {KnowledgeDraftRefinementService._norm(item["text"]) for item in claims}
        findings = []
        for text in article.get("checklist") or []:
            normalized = KnowledgeDraftRefinementService._norm(text)
            if normalized in supported or normalized.startswith("review the approved guidance in"):
                continue
            findings.append({"section": "procedure", "text": text, "material": True,
                             "reason": f"Material procedure is not traceable to authorized evidence: {text}"})
        return findings

    def _compose(self, package, sections, claims, evidence, reusable):
        article = deepcopy(package["draft_preview"])
        grouped: dict[str, list[str]] = {}
        for claim in claims:
            grouped.setdefault(claim["section"], []).append(claim["text"])
        if grouped.get("purpose"):
            article["overview"] = grouped["purpose"][0]
        if grouped.get("symptoms"):
            article["common_indicators"] = self._unique(grouped["symptoms"])
        if grouped.get("procedure"):
            article["checklist"] = self._unique(grouped["procedure"])
        article["related_topics"] = self._unique([
            *article.get("related_topics", []), *[item["title"] for item in reusable]
        ])
        article["refinement"] = {
            "package_id": package["package_id"], "method": "deterministic-evidence-refiner-v1",
            "sections": sections, "claim_traceability": claims,
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "reused_article_ids": [item["article_id"] for item in reusable],
        }
        article.setdefault("knowledge_factory", {})["refinement_method"] = "deterministic-evidence-refiner-v1"
        return article

    @staticmethod
    def _validation_results(refinement):
        validation = refinement["validation"]
        results = []
        results.append({"level": "passed" if not validation["article_schema_errors"] else "error",
                        "message": "Article schema validation passed." if not validation["article_schema_errors"] else
                                   "; ".join(validation["article_schema_errors"])})
        results.append({"level": "passed" if validation["provenance_valid"] else "error",
                        "message": "All refinement evidence is approved and traceable." if validation["provenance_valid"] else
                                   "Refinement evidence provenance is incomplete."})
        if validation["required_incomplete"]:
            results.append({"level": "error", "message": "Required sections need evidence: " +
                            ", ".join(validation["required_incomplete"])})
        if validation["material_unsupported_count"]:
            results.append({"level": "error", "message": "Material unsupported guidance blocks review."})
        return results

    @staticmethod
    def _completeness(sections):
        applicable = [item for item in sections if item["status"] != "not_applicable"]
        complete = [item for item in applicable if item["status"] == "supported"]
        return round(100 * len(complete) / len(applicable)) if applicable else 100

    @staticmethod
    def _event(package, event, at, **details):
        record = {"event": event, "at": at, "actor": "Deterministic Refinement Service", **details}
        comparable = {key: value for key, value in record.items() if key != "at"}
        if package.get("history") and {key: value for key, value in package["history"][-1].items()
                                       if key != "at"} == comparable:
            return
        package.setdefault("history", []).append(record)

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

    @staticmethod
    def _deduplicate_records(records):
        return list({item["evidence_id"]: item for item in records}.values())
