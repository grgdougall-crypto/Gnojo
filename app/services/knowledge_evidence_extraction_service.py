from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchError,
    KnowledgeSourceResearchService,
    SourceHTTPValidator,
    canonicalize_url,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)


EXTRACTION_STATUSES = (
    "proposed", "retrieving", "extracted", "needs_review", "partially_approved",
    "approved", "insufficient_evidence", "needs_refresh", "failed", "rejected", "superseded",
)
EVIDENCE_REVIEW_STATES = ("proposed", "approved", "rejected", "needs_revision")
EVIDENCE_ASSISTANCE_CATEGORIES = (
    "strongly_relevant", "supporting_context", "review_attention", "human_interpretation",
)
CANDIDACY_ROLES = ("candidate", "context")
CANDIDACY_RECOMMENDATIONS = ("candidate", "context", "undetermined")
CANDIDACY_RULE_VERSION = "deterministic-evidence-candidacy-v2"
LEGACY_CANDIDACY_RULE_VERSIONS = {"deterministic-evidence-candidacy-v1"}
CONTENT_DISPOSITION_RULE_VERSION = "deterministic-non-substantive-v1"
REVIEWABLE_DISPOSITION = "reviewable"
SUPPRESSED_DISPOSITION = "suppressed_non_substantive"

_ASSISTANCE_STOP_WORDS = {
    "and", "are", "campaign", "content", "coverage", "current", "evidence", "for",
    "gap", "governed", "improve", "missing", "review", "safety", "source", "the",
    "this", "work", "workflow", "item", "required", "production", "windows",
}


class KnowledgeEvidenceExtractionError(ValueError):
    pass


class _EvidenceParser(HTMLParser):
    """Small document parser that intentionally ignores page chrome."""

    BLOCKS = {"p", "li", "pre", "code"}
    HEADINGS = {"h1", "h2", "h3", "h4"}
    IGNORED = {"script", "style", "nav", "footer", "header", "form", "aside", "svg"}
    NON_CONTENT_HEADINGS = {
        "feedback", "relatedlinks", "commonparameters",
    }
    NON_CONTENT_TEXT = (
        "access to this page requires authorization",
        "you can try signing in or changing directories",
        "you can try changing directories",
        "need help with this topic",
        "want to try using ask learn",
    )

    def __init__(self):
        super().__init__()
        self.ignored_depth = 0
        self.active_tag: str | None = None
        self.parts: list[str] = []
        self.heading = ""
        self.blocks: list[dict[str, str]] = []
        self.link_depth = 0
        self.link_characters = 0
        self.link_count = 0
        self.structural_depth = 0
        self.structural_tags: list[str] = []
        self.block_structural = False

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attrs = {str(key).casefold(): str(value or "").casefold() for key, value in attrs}
        structural_tokens = f"{attrs.get('id', '')} {attrs.get('class', '')} {attrs.get('role', '')}"
        if any(token in structural_tokens for token in
               ("breadcrumb", "table-of-contents", "toc", "page-metadata", "navigation")):
            self.structural_depth += 1
            self.structural_tags.append(tag)
        if tag in self.IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCKS | self.HEADINGS:
            self.active_tag, self.parts = tag, []
            self.link_characters, self.link_count = 0, 0
            self.block_structural = bool(self.structural_depth)
        elif not self.ignored_depth and tag == "a" and self.active_tag:
            self.link_depth += 1
            self.link_count += 1

    def handle_endtag(self, tag):
        tag = tag.casefold()
        closes_structural = bool(self.structural_tags and tag == self.structural_tags[-1])
        if tag == "a" and self.link_depth:
            self.link_depth -= 1
            return
        if tag in self.IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth or tag != self.active_tag:
            if closes_structural:
                self.structural_tags.pop()
                self.structural_depth -= 1
            return
        text = " ".join(" ".join(self.parts).split())
        if tag in self.HEADINGS:
            self.heading = text[:180]
        elif len(text) >= 24:
            heading_key = re.sub(r"[^a-z0-9]+", " ", self.heading.casefold()).strip()
            text_key = " ".join(text.casefold().split())
            if (heading_key.replace(" ", "") not in self.NON_CONTENT_HEADINGS and
                    not any(marker in text_key for marker in self.NON_CONTENT_TEXT)):
                self.blocks.append({"tag": tag, "heading": self.heading, "text": text,
                                    "link_characters": self.link_characters,
                                    "link_count": self.link_count,
                                    "structural_context": self.block_structural})
        self.active_tag, self.parts = None, []
        if closes_structural:
            self.structural_tags.pop()
            self.structural_depth -= 1

    def handle_data(self, data):
        if not self.ignored_depth and self.active_tag:
            self.parts.append(data)
            if self.link_depth:
                self.link_characters += len(data.strip())


class KnowledgeEvidenceExtractionService:
    """Human-gated extraction from one already-approved research candidate."""

    MAX_PASSAGE = 420
    MAX_UNITS = 80
    EXTRACTION_METHOD = "deterministic-html-block-v3"

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 policy_path: Path | None = None,
                 taxonomy_path: Path | None = None,
                 http_validator: SourceHTTPValidator | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.package_root = self.campaign_root / "evidence_extraction"
        self.research = KnowledgeSourceResearchService(
            self.repository_root, self.campaign_root, policy_path=policy_path,
            taxonomy_path=taxonomy_path, http_validator=http_validator,
        )
        self.http_validator = http_validator or self.research.http_validator

    def list_for_research(self, research_package_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        values = [self._read(path) for path in self.package_root.glob("KEX-*.json")]
        return sorted((value for value in values
                       if value.get("research_package_id") == research_package_id),
                      key=lambda value: value.get("created_at", ""), reverse=True)

    def get(self, extraction_id: str) -> dict[str, Any]:
        path = self._path(extraction_id)
        if not path.exists():
            raise KnowledgeEvidenceExtractionError(
                f"Evidence extraction package '{extraction_id}' was not found."
            )
        return self._read(path)

    def reextraction_state(self, package_or_id: dict[str, Any] | str) -> dict[str, Any]:
        """Describe whether a supervised re-extraction is currently justified."""
        package = (self.get(package_or_id) if isinstance(package_or_id, str)
                   else package_or_id)
        units = package.get("evidence_units") or []
        methods = sorted({str(unit.get("extraction_method") or "unknown") for unit in units})
        version_stale = bool(units and methods != [self.EXTRACTION_METHOD])
        source_stale = package.get("status") == "needs_refresh"
        return {
            "available": version_stale or source_stale,
            "reason": "extractor_version" if version_stale else (
                "source_refresh" if source_stale else None
            ),
            "package_methods": methods,
            "package_method_label": ", ".join(methods) if methods else "not extracted",
            "current_method": self.EXTRACTION_METHOD,
        }

    def reextract(self, extraction_id: str) -> dict[str, Any]:
        """Run a justified human-triggered re-extraction, safely and idempotently."""
        package = self.get(extraction_id)
        if not self.reextraction_state(package)["available"]:
            return package
        return self.extract(extraction_id)

    def prepare(self, research_package_id: str, source_candidate_id: str) -> dict[str, Any]:
        research, candidate = self._eligible_candidate(research_package_id, source_candidate_id)
        url = canonicalize_url(candidate["canonical_url"])
        extraction_id = self._stable_id("KEX", research_package_id, source_candidate_id, url)
        path = self._path(extraction_id)
        if path.exists():
            return self._read(path)
        now = self._now()
        package = {
            "schema_version": "1.0", "extraction_id": extraction_id,
            "campaign_id": research["campaign_id"], "gap_id": research["gap_id"],
            "work_item_id": research["work_item_id"],
            "research_package_id": research_package_id,
            "source_candidate_id": source_candidate_id,
            "source_title": candidate.get("page_title") or "Authoritative source",
            "canonical_source_url": url, "publisher": candidate.get("publisher"),
            "authority_tier": candidate.get("authority_tier"),
            "platform": candidate.get("applicable_platform") or research.get("platform"),
            "product_vendor": candidate.get("applicable_product") or research.get("product_vendor"),
            "status": "proposed", "created_at": now, "updated_at": now,
            "extracted_at": None, "source_fingerprint": None,
            "retrieval": None, "evidence_units": [], "evidence_revisions": [],
            "candidacy": {
                "schema_version": "1.0", "rule_version": CANDIDACY_RULE_VERSION,
                "candidate_set_status": "unconfirmed", "confirmed_at": None,
                "confirmation_fingerprint": None,
            },
            "provenance": {
                "research_package_status": research["status"],
                "candidate_review_state": candidate["review_state"],
                "approved_content_digest": (candidate.get("provenance") or {}).get("content_digest"),
            },
            "history": [{"event": "extraction_prepared", "at": now, "actor": "Human"}],
        }
        self._save(package)
        return deepcopy(package)

    def extract(self, extraction_id: str) -> dict[str, Any]:
        package = self.get(extraction_id)
        self._eligible_candidate(package["research_package_id"], package["source_candidate_id"])
        if package.get("status") in {"rejected", "superseded"}:
            raise KnowledgeEvidenceExtractionError("Rejected or superseded extraction packages cannot run.")
        now = self._now()
        prior_units = deepcopy(package.get("evidence_units") or [])
        prior_methods = sorted({str(unit.get("extraction_method") or "unknown")
                                for unit in prior_units})
        prior_status = package.get("status")
        is_reextraction = bool(prior_units)
        try:
            inspected = self.http_validator.inspect(package["canonical_source_url"])
            final_url = canonicalize_url(inspected["final_url"])
            self._assert_related_destination(package, final_url)
            if inspected.get("content_type") not in {"text/html", "application/xhtml+xml"}:
                raise KnowledgeEvidenceExtractionError(
                    "This deterministic extractor currently supports HTML documents only."
                )
            fingerprint = inspected.get("content_digest") or self._fingerprint(
                inspected.get("content_preview", "")
            )
            methods = {unit.get("extraction_method") for unit in package.get("evidence_units") or []}
            if (package.get("source_fingerprint") == fingerprint and
                    package.get("evidence_units") and methods == {self.EXTRACTION_METHOD}):
                return deepcopy(package)
            package["status"] = "retrieving"
            self._event(package, "retrieval_started", now, actor="Human")
            self._save(package)
            if package.get("source_fingerprint") and package.get("evidence_units"):
                package["evidence_revisions"].append({
                    "source_fingerprint": package["source_fingerprint"],
                    "evidence_units": deepcopy(package["evidence_units"]),
                    "extraction_methods": prior_methods,
                    "package_status": prior_status,
                    "superseded_at": now,
                })
            units = self._extract_units(package, inspected.get("content_preview", ""), final_url)
            context = self._governed_context(package)
            for unit in units:
                unit["candidacy"] = self.candidacy_recommendation(unit, context)
            package["retrieval"] = {
                "requested_url": package["canonical_source_url"], "resolved_url": final_url,
                "http_status": inspected.get("http_status"), "retrieved_at": now,
                "content_type": inspected.get("content_type"),
                "source_title": inspected.get("page_title") or package["source_title"],
                "publisher": package.get("publisher"), "source_fingerprint": fingerprint,
                "redirect_chain": inspected.get("redirect_chain") or [], "result": "retrieved",
                "last_modified": inspected.get("last_modified"), "etag": inspected.get("etag"),
            }
            package["source_fingerprint"] = fingerprint
            package["evidence_units"] = units
            package["candidacy"] = self._empty_candidacy_state()
            package["extracted_at"] = now
            package["updated_at"] = now
            package["status"] = "needs_review"
            event = "evidence_reextracted" if is_reextraction else "evidence_extracted"
            self._event(package, event, now, actor="Deterministic Extractor",
                        evidence_count=len(units), source_fingerprint=fingerprint,
                        extraction_method=self.EXTRACTION_METHOD,
                        reviewable_count=sum(self._is_reviewable(unit) for unit in units),
                        suppressed_count=sum(not self._is_reviewable(unit) for unit in units),
                        content_disposition_rule_version=CONTENT_DISPOSITION_RULE_VERSION,
                        prior_extraction_methods=prior_methods if is_reextraction else [])
            self._event(
                package, "candidacy_recommended", now, actor="Deterministic Candidacy Rule",
                rule_version=CANDIDACY_RULE_VERSION,
                candidate_count=sum(
                    (unit.get("candidacy") or {}).get("machine_recommended_role") == "candidate"
                    for unit in units if self._is_reviewable(unit)
                ),
                context_count=sum(
                    (unit.get("candidacy") or {}).get("machine_recommended_role") == "context"
                    for unit in units if self._is_reviewable(unit)
                ),
                undetermined_count=sum(
                    (unit.get("candidacy") or {}).get("machine_recommended_role") == "undetermined"
                    for unit in units if self._is_reviewable(unit)
                ),
            )
        except (KnowledgeSourceResearchError, KnowledgeEvidenceExtractionError) as error:
            package["status"] = "failed"
            package["updated_at"] = now
            package["retrieval"] = {
                "requested_url": package["canonical_source_url"], "retrieved_at": now,
                "result": "failed", "reason": str(error),
            }
            self._event(package, "retrieval_failed", now, actor="Deterministic Extractor",
                        reason=str(error))
        self._save(package)
        return deepcopy(package)

    def review_evidence(self, extraction_id: str, evidence_id: str,
                        decision: str, notes: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected", "needs_revision"}:
            raise KnowledgeEvidenceExtractionError("Unknown evidence review decision.")
        package = self.get(extraction_id)
        unit = next((value for value in package.get("evidence_units", [])
                     if value.get("evidence_id") == evidence_id), None)
        if unit is None:
            raise KnowledgeEvidenceExtractionError("Evidence unit was not found.")
        if not self._is_reviewable(unit):
            raise KnowledgeEvidenceExtractionError(
                "Suppressed non-substantive material cannot receive an evidence decision."
            )
        if not self._candidate_set_current(package):
            raise KnowledgeEvidenceExtractionError(
                "Confirm the evidence candidate set before reviewing individual evidence."
            )
        if (unit.get("candidacy") or {}).get("human_confirmed_role") != "candidate":
            raise KnowledgeEvidenceExtractionError(
                "Only human-confirmed Candidate Evidence can receive an evidence decision."
            )
        notes = str(notes or "").strip()
        if unit.get("review_state") == decision and unit.get("reviewer_notes", "") == notes:
            return package
        now = self._now()
        unit["review_state"] = decision
        unit["reviewer_decision"] = decision
        unit["reviewer_notes"] = notes
        unit["reviewed_at"] = now
        package["status"] = self._review_status(package["evidence_units"], package)
        package["updated_at"] = now
        self._event(package, f"evidence_{decision}", now, actor="Human", evidence_id=evidence_id)
        self._save(package)
        return deepcopy(package)

    def set_candidacy_role(self, extraction_id: str, evidence_id: str,
                           role: str) -> dict[str, Any]:
        """Store one explicit human role decision without changing evidence review state."""
        if role not in CANDIDACY_ROLES:
            raise KnowledgeEvidenceExtractionError("Unknown candidacy role.")
        package = self.get(extraction_id)
        unit = next((value for value in package.get("evidence_units", [])
                     if value.get("evidence_id") == evidence_id), None)
        if unit is None:
            raise KnowledgeEvidenceExtractionError("Evidence unit was not found.")
        if not self._is_reviewable(unit):
            raise KnowledgeEvidenceExtractionError(
                "Suppressed non-substantive material does not require a candidacy decision."
            )
        candidacy = unit.setdefault("candidacy", self.candidacy_recommendation(
            unit, self._governed_context(package)
        ))
        previous = candidacy.get("human_confirmed_role")
        if previous == role:
            return package
        if previous == "candidate" and unit.get("review_state") != "proposed" and role == "context":
            raise KnowledgeEvidenceExtractionError(
                "Reviewed evidence cannot be reinterpreted as Reviewer Context."
            )
        now = self._now()
        candidacy["human_confirmed_role"] = role
        candidacy["role_decided_at"] = now
        candidacy["role_decided_by"] = "Human"
        state = package.setdefault("candidacy", self._empty_candidacy_state())
        if state.get("candidate_set_status") == "confirmed":
            state["candidate_set_status"] = "stale"
            state["stale_at"] = now
            state["stale_reason"] = "human_role_changed"
        event = "context_promoted" if previous == "context" and role == "candidate" \
            else "candidacy_role_changed"
        self._event(package, event, now, actor="Human", evidence_id=evidence_id,
                    previous_role=previous, role=role)
        package["status"] = self._review_status(package["evidence_units"], package)
        package["updated_at"] = now
        self._save(package)
        return deepcopy(package)

    def bulk_assign_visible_machine_context(
            self, extraction_id: str, *, review_state: str = "all",
            evidence_type: str = "all", assistance: str = "all",
            machine_recommendation: str = "all", human_role: str = "all",
            expected_count: int | None = None) -> dict[str, Any]:
        """Apply one explicit human bulk decision to eligible visible Context units only."""
        workspace = self.review_workspace(
            extraction_id, review_state=review_state, evidence_type=evidence_type,
            assistance=assistance, machine_recommendation=machine_recommendation,
            human_role=human_role,
        )
        eligible_ids = [unit["evidence_id"] for unit in workspace["units"]
                        if self._bulk_context_eligible(unit)]
        if expected_count is None or expected_count != len(eligible_ids):
            raise KnowledgeEvidenceExtractionError(
                "The visible machine-Context set changed. Review the count and try again."
            )
        if not eligible_ids:
            raise KnowledgeEvidenceExtractionError(
                "No unresolved, reviewable machine-Context units match the current filters."
            )
        return self._assign_machine_context(
            extraction_id, eligible_ids, event="machine_context_bulk_assigned",
            filters={"review_state": workspace["review_state"],
                     "evidence_type": workspace["evidence_type"],
                     "assistance": workspace["assistance"],
                     "machine_recommendation": workspace["machine_recommendation"],
                     "human_role": workspace["human_role"]},
        )

    def bulk_assign_all_machine_context(
            self, extraction_id: str, *, expected_count: int | None = None) -> dict[str, Any]:
        """Apply one explicit human decision to every eligible machine-Context unit."""
        workspace = self.review_workspace(extraction_id)
        eligible_ids = [unit["evidence_id"] for unit in workspace["units"]
                        if self._bulk_context_eligible(unit)]
        if expected_count is None or expected_count != len(eligible_ids):
            raise KnowledgeEvidenceExtractionError(
                "The unresolved machine-Context set changed. Review the count and try again."
            )
        if not eligible_ids:
            raise KnowledgeEvidenceExtractionError(
                "No unresolved, reviewable machine-Context units require assignment."
            )
        return self._assign_machine_context(
            extraction_id, eligible_ids, event="machine_context_bulk_assigned_all",
            filters={"scope": "all_reviewable_units"},
        )

    @staticmethod
    def _bulk_context_eligible(unit: dict[str, Any]) -> bool:
        candidacy = unit.get("candidacy") or {}
        return bool(
            KnowledgeEvidenceExtractionService._is_reviewable(unit)
            and (candidacy.get("human_confirmed_role") is None)
            and candidacy.get("machine_recommended_role") == "context"
        )

    def _assign_machine_context(
            self, extraction_id: str, eligible_ids: list[str], *, event: str,
            filters: dict[str, Any]) -> dict[str, Any]:
        """Revalidate and persist a bounded human Context assignment."""
        package = self.get(extraction_id)
        now = self._now()
        wanted = set(eligible_ids)
        for unit in package.get("evidence_units") or []:
            if unit.get("evidence_id") not in wanted:
                continue
            if not self._bulk_context_eligible(unit):
                raise KnowledgeEvidenceExtractionError(
                    "The machine-Context set changed. No decisions were saved."
                )
        for unit in package.get("evidence_units") or []:
            if unit.get("evidence_id") in wanted:
                unit["candidacy"].update(
                    human_confirmed_role="context", role_decided_at=now,
                    role_decided_by="Human",
                )
        package["updated_at"] = now
        package["status"] = self._review_status(package["evidence_units"], package)
        self._event(
            package, event, now, actor="Human",
            count=len(eligible_ids), evidence_ids=eligible_ids,
            filters=filters,
            candidacy_rule_version=(package.get("candidacy") or {}).get("rule_version"),
        )
        self._save(package)
        return deepcopy(package)

    def confirm_candidate_set(self, extraction_id: str) -> dict[str, Any]:
        """Finalize the human-selected candidate/context partition; approve nothing."""
        package = self.get(extraction_id)
        unresolved = [unit["evidence_id"] for unit in package.get("evidence_units", [])
                      if self._is_reviewable(unit)
                      if (unit.get("candidacy") or {}).get("human_confirmed_role")
                      not in CANDIDACY_ROLES]
        if unresolved:
            raise KnowledgeEvidenceExtractionError(
                "Assign every extracted unit to Candidate Evidence or Reviewer Context first."
            )
        state = package.setdefault("candidacy", self._empty_candidacy_state())
        rule_version = state.get("rule_version") or CANDIDACY_RULE_VERSION
        fingerprint = self._candidate_set_fingerprint(package, rule_version=rule_version)
        if (state.get("candidate_set_status") == "confirmed" and
                state.get("confirmation_fingerprint") == fingerprint):
            return package
        now = self._now()
        state.update({"schema_version": "1.0", "rule_version": rule_version,
                      "candidate_set_status": "confirmed", "confirmed_at": now,
                      "confirmed_by": "Human", "confirmation_fingerprint": fingerprint})
        candidate_count = sum(
            (unit.get("candidacy") or {}).get("human_confirmed_role") == "candidate"
            for unit in package["evidence_units"]
        )
        context_count = sum(
            (unit.get("candidacy") or {}).get("human_confirmed_role") == "context"
            for unit in package["evidence_units"]
        )
        state["candidate_set_outcome"] = "non_empty" if candidate_count else "empty"
        state.pop("stale_at", None)
        state.pop("stale_reason", None)
        package["status"] = self._review_status(package["evidence_units"], package)
        package["updated_at"] = now
        self._event(package, "candidate_set_confirmed", now, actor="Human",
                    confirmation_fingerprint=fingerprint,
                    candidate_set_outcome=state["candidate_set_outcome"],
                    candidate_count=candidate_count, context_count=context_count)
        self._save(package)
        return deepcopy(package)

    @classmethod
    def candidacy_recommendation(cls, unit: dict[str, Any],
                                 context: dict[str, Any]) -> dict[str, Any]:
        """Conservatively recommend a role; this is reproducible and non-authoritative."""
        if not cls._is_reviewable(unit):
            return {
                "machine_recommended_role": "context",
                "machine_rationale": (
                    "Deterministic structural rules identified non-substantive source material; "
                    "it remains preserved for traceability and is excluded from candidacy review."
                ),
                "rule_version": CANDIDACY_RULE_VERSION,
                "recommendation_fingerprint": cls._fingerprint({
                    "rule_version": CANDIDACY_RULE_VERSION,
                    "evidence_fingerprint": unit.get("fingerprint"),
                    "disposition": unit.get("content_disposition"),
                }),
                "human_confirmed_role": None, "role_decided_at": None,
                "role_decided_by": None,
            }
        assistance = cls.evidence_review_assistance(unit, context)
        role = assistance.get("recommended_role") or "undetermined"
        if role in {"candidate", "context"}:
            rationale = assistance["explanation"]
        else:
            rationale = ("Deterministic metadata cannot safely distinguish evidence from "
                         "reviewer context; human assignment is required.")
        return {
            "machine_recommended_role": role, "machine_rationale": rationale,
            "rule_version": CANDIDACY_RULE_VERSION,
            "recommendation_fingerprint": cls._fingerprint({
                "rule_version": CANDIDACY_RULE_VERSION,
                "evidence_fingerprint": unit.get("fingerprint"),
                "context": cls._candidacy_context_fingerprint(context),
            }),
            "human_confirmed_role": None, "role_decided_at": None,
            "role_decided_by": None,
        }

    @staticmethod
    def _assistance_terms(context: dict[str, Any]) -> list[str]:
        """Return explainable objective terms, not a synthetic relevance score."""
        values = (
            context.get("area"), context.get("gap_summary"), context.get("gap_type"),
            context.get("work_type"), context.get("campaign_objective"),
        )
        tokens: set[str] = set()
        for value in values:
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]*", str(value or "")):
                normalized = token.casefold().strip(".-")
                if len(normalized) >= 3 and normalized not in _ASSISTANCE_STOP_WORDS:
                    tokens.add(normalized)
        return sorted(tokens, key=lambda value: (-len(value), value))

    @classmethod
    def evidence_review_assistance(cls, unit: dict[str, Any],
                                   context: dict[str, Any]) -> dict[str, Any]:
        """Derive non-persistent review help from authoritative context and evidence."""
        claim = str(unit.get("normalized_claim") or "")
        passage = str(unit.get("supporting_passage") or "")
        heading = str((unit.get("source_location") or {}).get("heading") or "")
        haystack = f"{claim} {passage}".casefold()
        matched = [term for term in cls._assistance_terms(context)
                   if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack)]
        evidence_type = str(unit.get("evidence_type") or "unspecified")
        expected_platform = str(context.get("platform") or "").casefold()
        actual_platform = str(unit.get("platform_applicability") or "").casefold()
        platform_mismatch = bool(expected_platform and actual_platform and
                                 expected_platform not in actual_platform and
                                 "cross" not in actual_platform)
        objective_text = " ".join(str(context.get(key) or "") for key in
                                  ("gap_type", "work_type", "facet", "gap_summary")).casefold()
        safety_objective = any(marker in objective_text for marker in
                               ("missing_safety", "safety_review", "safety authorization",
                                "safety_authorization", "missing safety"))
        safety_signal = bool(re.search(
            r"\b(authori[sz](?:e|ed|ation)|administrator|elevated|permission|privilege|"
            r"warning|caution|risk|interrupt|disconnect|backup|restore|rollback|avoid|"
            r"do not|only (?:if|from)|requires?)\b", haystack))
        verification_signal = bool(re.search(
            r"\b(verify|confirm|expected result|returns?|responds?|successful|failure|"
            r"observe|output|result)\b", haystack))

        if platform_mismatch:
            category = "review_attention"
            label = "Review Attention"
            explanation = (
                f"This unit is marked for {unit.get('platform_applicability')}, while the governed "
                f"work item is scoped to {context.get('platform')}; confirm its applicability."
            )
            role = "Applicability review"
            recommended_role = "undetermined"
        elif safety_objective and matched and safety_signal and evidence_type in {
                "safety", "preconditions", "authorization_requirements", "verification",
                "expected_result", "diagnostic_observations"}:
            category = "strongly_relevant"
            label = "Strongly Relevant"
            explanation = (
                "The proposition contains an explicit safety or authorization boundary and its "
                "evidence type is compatible with the governed safety objective."
            )
            role = "Objective-specific safety evidence"
            recommended_role = "candidate"
        elif safety_objective:
            category = "supporting_context" if evidence_type in {
                "diagnostic_observations", "procedure", "commands", "preconditions"
            } else "human_interpretation"
            label = ("Supporting / Contextual" if category == "supporting_context"
                     else "Human Interpretation Required")
            explanation = (
                "The source material may describe the broader procedure, but the proposition does "
                "not state a safety, authorization, or verification boundary required by this gap."
            )
            role = "Supporting technical context"
            recommended_role = "context" if category == "supporting_context" else "undetermined"
        elif matched and (verification_signal or evidence_type in {
                "safety", "authorization_requirements", "verification", "expected_result",
                "platform_applicability", "escalation"}):
            category = "strongly_relevant"
            label = "Strongly Relevant"
            explanation = (
                "The proposition uses governed objective concepts and contains an evidence-specific "
                "observation, boundary, or expected result."
            )
            role = "Objective-specific technical evidence"
            recommended_role = "candidate"
        elif evidence_type == "commands":
            category = "review_attention"
            label = "Review Attention"
            explanation = (
                "This unit contains command syntax from the approved source but does not directly "
                "use the governed objective terms; confirm whether a command reference is useful."
            )
            role = "Command reference candidate"
            recommended_role = "undetermined"
        elif evidence_type in {"diagnostic_observations", "procedure", "preconditions"}:
            category = "supporting_context"
            label = "Supporting / Contextual"
            area = str(context.get("area") or "the current")
            type_label = {
                "diagnostic_observations": "diagnostic observation",
                "procedure": "procedure", "preconditions": "prerequisite",
            }[evidence_type]
            explanation = (
                f"This {type_label} comes from the approved source but "
                f"does not directly use the governed {area} terms; assess whether it supplies "
                "useful supporting context."
            )
            role = "Supporting technical context"
            recommended_role = "context"
        else:
            category = "human_interpretation"
            label = "Human Interpretation Required"
            explanation = (
                "Available structured metadata does not establish a sufficiently direct connection "
                "to the governed objective; determine its role during human review."
            )
            role = "Human classification required"
            recommended_role = "undetermined"
        return {
            "category": category, "label": label, "explanation": explanation,
            "potential_role": role, "matched_terms": matched[:3],
            "recommended_role": recommended_role,
        }

    @classmethod
    def candidate_purpose(cls, unit: dict[str, Any],
                          context: dict[str, Any]) -> dict[str, str]:
        """Explain a possible downstream use without making a candidacy decision."""
        evidence_type = str(unit.get("evidence_type") or "unspecified")
        claim = str(unit.get("normalized_claim") or "").strip()
        passage = str(unit.get("supporting_passage") or "").strip()
        heading = str((unit.get("source_location") or {}).get("heading") or "").strip()
        text = f"{heading} {claim} {passage}".casefold()
        objective = str(context.get("gap_summary") or context.get("campaign_objective")
                        or "the governed work item").strip().rstrip(".!? ")

        if evidence_type == "authorization_requirements" or re.search(
                r"\b(authori[sz](?:e|ed|ation)|administrator|permission|privilege)\b", text):
            category = "Authorization boundary"
            why = ("This evidence identifies an authorization or permission boundary that may "
                   f"constrain safe work for the governed objective: {objective}.")
        elif evidence_type in {"safety", "preconditions"}:
            category = "Safety prerequisite"
            why = ("This evidence states a prerequisite or caution that may need to be satisfied "
                   f"before work proceeds on {objective}.")
        elif evidence_type == "platform_applicability":
            category = "Platform applicability"
            why = ("This evidence defines where the documented behavior applies, which may help "
                   f"a reviewer keep {objective} within the supported platform scope.")
        elif evidence_type == "escalation":
            category = "Escalation support"
            why = ("This evidence describes an escalation condition or destination that may support "
                   f"a governed handoff for {objective}.")
        elif evidence_type == "expected_result":
            category = "Expected-result support"
            why = ("This evidence describes an observable result that may support verification of "
                   f"the governed work item: {objective}.")
        elif evidence_type == "verification":
            category = "Diagnostic verification"
            why = ("This evidence provides a verification statement that may help a reviewer decide "
                   f"whether {objective} has been demonstrated.")
        elif evidence_type == "commands":
            scoped = bool(re.search(
                r"\b(interface(alias|index)?|compartment|pipeline|input|target|select)\b", text))
            category = "Scope/target selection" if scoped else "Command interpretation"
            if scoped:
                why = ("This command syntax or example shows how execution can be scoped to a "
                       f"particular interface or input for the governed objective: {objective}.")
            else:
                why = ("This command syntax or example may help a reviewer interpret how the "
                       f"documented command relates to {objective}; it does not by itself prove the result.")
        elif evidence_type == "diagnostic_observations" and re.search(
                r"\b(configuration|configured|property|properties|address|server|status|value)\b", text):
            category = "Configuration verification"
            why = ("This evidence identifies configuration information exposed by the documented "
                   f"tool that may be observed when verifying {objective}.")
        elif evidence_type in {"diagnostic_observations", "procedure"}:
            category = "Supporting procedural context"
            why = ("This observation or procedure may provide background for the governed work "
                   f"item, but its metadata does not establish a specific evidentiary purpose for {objective}.")
        else:
            category = "Human interpretation required"
            why = ("The available evidence type and source context do not establish a sufficiently "
                   "specific downstream purpose; a reviewer must determine whether it supports the "
                   "governed work item.")
        return {"category": category, "explanation": why}

    def review_workspace(self, extraction_id: str, *, review_state: str = "all",
                         evidence_type: str = "all", assistance: str = "all",
                         machine_recommendation: str = "all", human_role: str = "all",
                         candidacy_role: str | None = None) -> dict[str, Any]:
        """Project authoritative package state into a read-only human review workspace."""
        package = self.get(extraction_id)
        units = package.get("evidence_units") or []
        states = {state: 0 for state in EVIDENCE_REVIEW_STATES}
        reviewable_units = [unit for unit in units if self._is_reviewable(unit)]
        suppressed_units = [deepcopy(unit) for unit in units if not self._is_reviewable(unit)]
        evidence_types = sorted({str(unit.get("evidence_type") or "unspecified")
                                 for unit in reviewable_units})
        selected_state = review_state if review_state in {"all", *EVIDENCE_REVIEW_STATES} else "all"
        selected_type = evidence_type if evidence_type in {"all", *evidence_types} else "all"
        context = self._governed_context(package)
        candidacy_roles = {"candidate", "context", "unresolved"}
        recommendation_roles = {"candidate", "context", "undetermined"}
        # ``candidacy_role`` is a compatibility alias for callers predating the
        # split filter UI. It remains read-only and maps only to the human role.
        requested_human_role = (candidacy_role if candidacy_role is not None else human_role)
        selected_human_role = (requested_human_role
                               if requested_human_role in {"all", *candidacy_roles} else "all")
        selected_recommendation = (machine_recommendation
                                   if machine_recommendation in {"all", *recommendation_roles}
                                   else "all")
        candidate_current = self._candidate_set_current(package)
        try:
            campaign = KnowledgeCoveragePlannerService(
                self.repository_root, self.campaign_root
            ).get(str(package.get("campaign_id") or ""))
            work = next((item for item in campaign.get("work_items") or []
                         if item.get("work_item_id") == package.get("work_item_id")), {})
            gap = next((item for item in campaign.get("gaps") or []
                        if item.get("gap_id") == package.get("gap_id")), {})
            context.update(
                campaign_title=campaign.get("title") or context["campaign_title"],
                campaign_objective=campaign.get("objective") or "",
                area=work.get("area_id") or gap.get("area") or campaign.get("scope") or context["area"],
                platform=work.get("platform") or package.get("platform") or
                ((campaign.get("platforms") or [""])[0]),
                work_type=work.get("work_type") or context["work_type"],
                gap_type=gap.get("gap_type") or context["gap_type"],
                facet=(gap.get("facet") or work.get("facet") or
                       work.get("objective_facet") or context.get("facet") or ""),
                gap_summary=gap.get("summary") or work.get("reason") or
                work.get("title") or context["gap_summary"],
            )
        except KnowledgeCoveragePlannerError:
            pass
        selected_assistance = assistance if assistance in {
            "all", *EVIDENCE_ASSISTANCE_CATEGORIES
        } else "all"
        decorated = []
        assistance_counts = {category: 0 for category in EVIDENCE_ASSISTANCE_CATEGORIES}
        for unit in reviewable_units:
            projected = deepcopy(unit)
            projected["review_assistance"] = self.evidence_review_assistance(projected, context)
            projected.setdefault("candidacy", self.candidacy_recommendation(projected, context))
            projected["machine_recommendation"] = (
                projected["candidacy"].get("machine_recommended_role") or "undetermined"
            )
            projected["candidate_purpose"] = (
                self.candidate_purpose(projected, context)
                if projected["machine_recommendation"] == "candidate" else None
            )
            projected["candidacy_role"] = (projected["candidacy"].get("human_confirmed_role")
                                           or "unresolved")
            assistance_counts[projected["review_assistance"]["category"]] += 1
            decorated.append(projected)
        candidate_units = [u for u in decorated if u["candidacy_role"] == "candidate"]
        states = {state: sum(unit.get("review_state") == state for unit in candidate_units)
                  for state in EVIDENCE_REVIEW_STATES}
        reviewed = states["approved"] + states["rejected"] + states["needs_revision"]
        order = {category: index for index, category in enumerate(EVIDENCE_ASSISTANCE_CATEGORIES)}
        decorated.sort(key=lambda unit: (
            order[unit["review_assistance"]["category"]],
            next((index for index, value in enumerate(units)
                  if value.get("evidence_id") == unit.get("evidence_id")), len(units)),
        ))
        filtered = [unit for unit in decorated
                    if (selected_state == "all" or unit.get("review_state") == selected_state)
                    and (selected_type == "all" or
                         str(unit.get("evidence_type") or "unspecified") == selected_type)
                    and (selected_assistance == "all" or
                         unit["review_assistance"]["category"] == selected_assistance)
                    and (selected_recommendation == "all" or
                         unit["machine_recommendation"] == selected_recommendation)
                    and (selected_human_role == "all" or
                         unit["candidacy_role"] == selected_human_role)]
        bulk_context_eligible = [
            unit for unit in filtered
            if self._bulk_context_eligible(unit)
        ]
        all_bulk_context_eligible = [unit for unit in decorated
                                     if self._bulk_context_eligible(unit)]
        next_undecided = next((unit.get("evidence_id") for unit in filtered
                              if unit.get("review_state") == "proposed" and
                              unit.get("candidacy_role") == "candidate"), None)
        group_units = [unit for unit in decorated
                       if unit["candidacy_role"] == "candidate"
                       and (selected_type == "all" or
                           str(unit.get("evidence_type") or "unspecified") == selected_type)
                       and (selected_assistance == "all" or
                            unit["review_assistance"]["category"] == selected_assistance)]
        group_remaining = sum(unit.get("review_state") == "proposed" for unit in group_units)
        unresolved_count = sum(u["candidacy_role"] == "unresolved" for u in decorated)
        recommendation_counts = {
            role: sum(unit["machine_recommendation"] == role for unit in decorated)
            for role in recommendation_roles
        }
        human_role_counts = {
            role: sum(unit["candidacy_role"] == role for unit in decorated)
            for role in candidacy_roles
        }
        candidate_count = human_role_counts["candidate"]
        candidacy_ready_to_confirm = unresolved_count == 0
        filters_active = any(value != "all" for value in (
            selected_state, selected_type, selected_assistance,
            selected_recommendation, selected_human_role,
        ))
        scoped_group_active = any(value != "all" for value in (
            selected_type, selected_assistance, selected_recommendation, selected_human_role,
        ))
        return {
            "package": package, "context": context, "units": filtered,
            "suppressed_units": suppressed_units,
            "suppressed_count": len(suppressed_units),
            "bulk_context_eligible_count": len(bulk_context_eligible),
            "all_bulk_context_eligible_count": len(all_bulk_context_eligible),
            "reviewable_count": len(reviewable_units),
            "counts": {**states, "total": len(candidate_units), "reviewed": reviewed,
                       "remaining": states["proposed"]},
            "review_state": selected_state, "evidence_type": selected_type,
            "evidence_types": evidence_types, "assistance": selected_assistance,
            "machine_recommendation": selected_recommendation,
            "machine_recommendations": sorted(recommendation_roles),
            "machine_recommendation_counts": recommendation_counts,
            "human_role": selected_human_role, "human_roles": sorted(candidacy_roles),
            # Compatibility projection for existing callers; new UI uses human_role.
            "candidacy_role": selected_human_role, "candidacy_roles": sorted(candidacy_roles),
            "candidate_set_current": candidate_current, "unresolved_candidacy": unresolved_count,
            "candidate_count": candidate_count,
            "candidate_set_empty": candidate_count == 0,
            "candidacy_ready_to_confirm": candidacy_ready_to_confirm,
            "assistance_categories": EVIDENCE_ASSISTANCE_CATEGORIES,
            "assistance_counts": assistance_counts, "next_undecided": next_undecided,
            "filters_active": filters_active, "next_matching": (
                filtered[0].get("evidence_id") if filtered else None
            ),
            "human_role_counts": human_role_counts,
            "group_remaining": group_remaining,
            "group_complete": bool(reviewable_units) and scoped_group_active and not filtered,
            "complete": self._review_complete(package),
        }

    def refresh_status(self, extraction_id: str) -> dict[str, Any]:
        """Human-initiated staleness check; it never replaces approved evidence."""
        package = self.get(extraction_id)
        try:
            inspected = self.http_validator.inspect(package["canonical_source_url"])
            fingerprint = inspected.get("content_digest")
            if fingerprint and package.get("source_fingerprint") and fingerprint != package["source_fingerprint"]:
                package["status"] = "needs_refresh"
                now = self._now()
                package["updated_at"] = now
                self._event(package, "source_change_detected", now,
                            actor="Deterministic Extractor", observed_fingerprint=fingerprint)
                self._save(package)
        except KnowledgeSourceResearchError:
            pass
        return deepcopy(package)

    def approved_units_for(self, research_package_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(research_package_ids)
        if not wanted or not self.package_root.exists():
            return []
        units: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in sorted(self.package_root.glob("KEX-*.json")):
            package = self._read(path)
            if package.get("research_package_id") not in wanted:
                continue
            if package.get("status") != "approved" or not self._candidate_set_current(package):
                continue
            for unit in package.get("evidence_units") or []:
                if (unit.get("review_state") != "approved" or
                        not self._is_reviewable(unit) or
                        (unit.get("candidacy") or {}).get("human_confirmed_role") != "candidate" or
                        unit["evidence_id"] in seen):
                    continue
                seen.add(unit["evidence_id"])
                units.append(deepcopy(unit))
        return units

    def _eligible_candidate(self, research_package_id: str, source_candidate_id: str):
        try:
            research = self.research.get(research_package_id)
        except KnowledgeSourceResearchError as error:
            raise KnowledgeEvidenceExtractionError(str(error)) from error
        candidate = next((value for value in research.get("candidate_sources", [])
                          if value.get("source_candidate_id") == source_candidate_id), None)
        if research.get("status") != "approved" or candidate is None:
            raise KnowledgeEvidenceExtractionError(
                "Evidence extraction requires an approved Phase 2 research package and source."
            )
        if (source_candidate_id not in set(research.get("selected_sources") or []) or
                candidate.get("review_state") != "selected" or
                candidate.get("topic_relevant") is not True or
                candidate.get("authority_tier") not in {1, 2}):
            raise KnowledgeEvidenceExtractionError(
                "Only a human-selected, topic-relevant Tier 1 or Tier 2 source is eligible."
            )
        return research, candidate

    def _assert_related_destination(self, package: dict[str, Any], final_url: str) -> None:
        original_host = (urlsplit(package["canonical_source_url"]).hostname or "").casefold()
        final_host = (urlsplit(final_url).hostname or "").casefold()
        related = (original_host == final_host or original_host.endswith(f".{final_host}") or
                   final_host.endswith(f".{original_host}"))
        authority = self.research.policy.classify(final_url)
        same_publisher = (authority.get("publisher") and package.get("publisher") and
                          authority["publisher"].casefold() == package["publisher"].casefold())
        if not related and not same_publisher:
            raise KnowledgeEvidenceExtractionError("Approved source redirected to an unrelated destination.")
        if authority.get("authority_tier") not in {1, 2}:
            raise KnowledgeEvidenceExtractionError("Resolved source is no longer an approved authority.")

    def _extract_units(self, package: dict[str, Any], html: str, source_url: str) -> list[dict[str, Any]]:
        parser = _EvidenceParser()
        parser.feed(html)
        units, seen = [], set()
        for index, block in enumerate(parser.blocks[: self.MAX_UNITS * 3]):
            text = block["text"][: self.MAX_PASSAGE].strip()
            normalized = re.sub(r"\s+", " ", text)
            key = normalized.casefold()
            if not normalized or key in seen:
                continue
            seen.add(key)
            evidence_type = self._classify(block["tag"], block["heading"], normalized)
            disposition = self._content_disposition(block, normalized)
            evidence_id = self._stable_id(
                "EVD", package["extraction_id"], evidence_type, normalized.casefold()
            )
            units.append({
                "evidence_id": evidence_id, "evidence_type": evidence_type,
                "normalized_claim": normalized, "supporting_passage": text,
                "source_location": {"heading": block["heading"], "block_index": index,
                                    "html_element": block["tag"]},
                "source_url": source_url, "source_title": package["source_title"],
                "publisher": package.get("publisher"),
                "platform_applicability": package.get("platform") or "Unspecified",
                "confidence": "medium", "extraction_method": self.EXTRACTION_METHOD,
                "review_state": "proposed", "reviewer_decision": None,
                "reviewer_notes": "", "reviewed_at": None,
                "fingerprint": self._fingerprint({"text": normalized, "type": evidence_type}),
                "provenance": {"extraction_id": package["extraction_id"],
                               "research_package_id": package["research_package_id"],
                               "source_candidate_id": package["source_candidate_id"]},
                "content_disposition": disposition,
            })
            if len(units) >= self.MAX_UNITS:
                break
        if not units:
            raise KnowledgeEvidenceExtractionError("No bounded technical evidence could be extracted safely.")
        return units

    @staticmethod
    def _content_disposition(block: dict[str, Any], text: str) -> dict[str, Any]:
        """Classify only structurally or lexically provable non-substantive material."""
        normalized = " ".join(str(text or "").split()).strip()
        folded = normalized.casefold().rstrip(". ")
        reason = None
        basis = None
        if block.get("structural_context"):
            reason, basis = "source_navigation_container", "structural"
        elif re.fullmatch(r"(?:for more information,?\s*(?:see)?|see also)\s*: ?", folded):
            reason, basis = "cross_reference_lead_in", "exact_text_pattern"
        elif re.fullmatch(
                r"(?:last\s+)?(?:updated|modified|published)(?:\s+on)?\s*:?\s*"
                r"\d{4}-\d{2}-\d{2}", folded):
            reason, basis = "document_date_metadata", "exact_text_pattern"
        elif (block.get("tag") == "li" and block.get("link_count")
              and int(block.get("link_characters") or 0) >= len(normalized.replace(" ", "")) * .8
              and re.match(r"^(?:chapter|section)\s+\d+\b", folded)):
            reason, basis = "linked_table_of_contents_entry", "structural_link_ratio"
        return {
            "status": SUPPRESSED_DISPOSITION if reason else REVIEWABLE_DISPOSITION,
            "reason": reason, "basis": basis,
            "rule_version": CONTENT_DISPOSITION_RULE_VERSION,
        }

    @staticmethod
    def _is_reviewable(unit: dict[str, Any]) -> bool:
        return (unit.get("content_disposition") or {}).get(
            "status", REVIEWABLE_DISPOSITION) != SUPPRESSED_DISPOSITION

    @staticmethod
    def _classify(tag: str, heading: str, text: str) -> str:
        value = f"{heading} {text}".casefold()
        if tag in {"pre", "code"}:
            return "commands"
        if any(term in value for term in ("warning", "caution", "important", "administrator", "back up")):
            return "safety"
        if any(term in value for term in ("prerequisite", "before you", "requires", "you need")):
            return "preconditions"
        if any(term in value for term in ("verify", "verification", "confirm", "make sure")):
            return "verification"
        if any(term in value for term in ("expected result", "should now", "after completing")):
            return "expected_result"
        if any(term in value for term in ("if this does not", "otherwise", "alternate", "instead")):
            return "alternate_outcomes"
        if any(term in value for term in ("contact support", "escalat", "manufacturer support")):
            return "escalation"
        if any(term in value for term in ("windows 10", "windows 11", "macos", "linux", "applies to", "version")):
            return "platform_applicability"
        if any(term in value for term in ("symptom", "error", "unable to", "doesn't", "does not", "fails")):
            return "symptoms"
        return "procedure" if tag == "li" else "diagnostic_observations"

    def _review_status(self, units: list[dict[str, Any]], package: dict[str, Any] | None = None) -> str:
        if package is not None and not self._candidate_set_current(package):
            return "needs_review"
        candidates = [unit for unit in units
                      if (unit.get("candidacy") or {}).get("human_confirmed_role") == "candidate"]
        if not candidates:
            return "insufficient_evidence"
        states = [unit.get("review_state", "proposed") for unit in candidates]
        if states and "proposed" not in states and "approved" in states:
            return "approved"
        if any(state == "approved" for state in states):
            return "partially_approved"
        return "needs_review"

    def _review_complete(self, package: dict[str, Any]) -> bool:
        if not self._candidate_set_current(package):
            return False
        candidates = [unit for unit in package.get("evidence_units", [])
                      if (unit.get("candidacy") or {}).get("human_confirmed_role") == "candidate"]
        return bool(candidates) and all(unit.get("review_state") != "proposed" for unit in candidates) \
            and any(unit.get("review_state") == "approved" for unit in candidates)

    @staticmethod
    def _empty_candidacy_state() -> dict[str, Any]:
        return {"schema_version": "1.0", "rule_version": CANDIDACY_RULE_VERSION,
                "candidate_set_status": "unconfirmed", "confirmed_at": None,
                "confirmation_fingerprint": None}

    def _governed_context(self, package: dict[str, Any]) -> dict[str, Any]:
        context = {"campaign_id": package.get("campaign_id"), "campaign_title": "Campaign",
                   "area": "Not specified", "work_item_id": package.get("work_item_id"),
                   "work_type": "Not specified", "gap_type": "Not specified", "facet": "",
                   "campaign_objective": "", "platform": package.get("platform") or "",
                   "gap_summary": "Review whether each source statement supports the governed work item."}
        try:
            campaign = KnowledgeCoveragePlannerService(self.repository_root, self.campaign_root).get(
                str(package.get("campaign_id") or ""))
            work = next((x for x in campaign.get("work_items", [])
                         if x.get("work_item_id") == package.get("work_item_id")), {})
            gap = next((x for x in campaign.get("gaps", [])
                        if x.get("gap_id") == package.get("gap_id")), {})
            context.update(campaign_title=campaign.get("title") or "Campaign",
                           campaign_objective=campaign.get("objective") or "",
                           area=work.get("area_id") or gap.get("area") or campaign.get("scope") or "Not specified",
                           platform=work.get("platform") or package.get("platform") or "",
                           work_type=work.get("work_type") or "Not specified",
                           gap_type=gap.get("gap_type") or "Not specified",
                           facet=(gap.get("facet") or work.get("facet") or
                                  work.get("objective_facet") or ""),
                           gap_summary=gap.get("summary") or work.get("reason") or work.get("title") or context["gap_summary"])
        except KnowledgeCoveragePlannerError:
            pass
        return context

    @staticmethod
    def _candidacy_context_fingerprint(context: dict[str, Any]) -> dict[str, Any]:
        return {key: context.get(key) for key in
                ("campaign_id", "work_item_id", "area", "work_type", "gap_type",
                 "campaign_objective", "platform", "gap_summary")}

    def _candidate_set_fingerprint(self, package: dict[str, Any],
                                   rule_version: str | None = None) -> str:
        rule_version = rule_version or CANDIDACY_RULE_VERSION
        context = self._candidacy_context_fingerprint(self._governed_context(package))
        if rule_version == CANDIDACY_RULE_VERSION:
            context["facet"] = self._governed_context(package).get("facet")
            units = sorted((u.get("evidence_id"), u.get("fingerprint"),
                            (u.get("content_disposition") or {}).get("status",
                                                                     REVIEWABLE_DISPOSITION),
                            (u.get("candidacy") or {}).get("human_confirmed_role"))
                           for u in package.get("evidence_units", []))
        else:
            units = sorted((u.get("evidence_id"), u.get("fingerprint"),
                            (u.get("candidacy") or {}).get("human_confirmed_role"))
                           for u in package.get("evidence_units", []))
        return self._fingerprint({"source": package.get("source_fingerprint"),
            "revision": package.get("revision"), "rule": rule_version,
            "context": context, "units": units})

    def _candidate_set_current(self, package: dict[str, Any]) -> bool:
        state = package.get("candidacy") or {}
        rule_version = state.get("rule_version")
        return (state.get("candidate_set_status") == "confirmed" and
                rule_version in {CANDIDACY_RULE_VERSION, *LEGACY_CANDIDACY_RULE_VERSIONS} and
                state.get("confirmation_fingerprint") == self._candidate_set_fingerprint(
                    package, rule_version=rule_version))

    def _path(self, extraction_id: str) -> Path:
        if not re.fullmatch(r"KEX-[A-F0-9]{12}", str(extraction_id or "")):
            raise KnowledgeEvidenceExtractionError("Invalid evidence extraction ID.")
        return self.package_root / f"{extraction_id}.json"

    def _save(self, package: dict[str, Any]) -> None:
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(package["extraction_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(package, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeEvidenceExtractionError(f"Unable to read extraction package: {error}") from error

    @staticmethod
    def _event(package: dict[str, Any], event: str, at: str, **values) -> None:
        record = {"event": event, "at": at, **values}
        if not package.get("history") or package["history"][-1] != record:
            package.setdefault("history", []).append(record)

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        return f"{prefix}-{hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:12].upper()}"

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
