from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from app.data_root import resolve_data_root
from typing import Any
from urllib.parse import urlsplit

from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchError,
    KnowledgeSourceResearchService,
    SourceHTTPValidator,
    canonicalize_url,
    sanitize_source_title,
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
CONTENT_DISPOSITION_RULE_VERSION = "deterministic-non-substantive-v2"
SOURCE_CONTENT_POLICY = "bounded-full-response-v1"
WORKFLOW_PROPOSITION_POLICY = "deterministic-workflow-propositions-v1"
WORKFLOW_EVIDENCE_COMPRESSION_POLICY = "deterministic-workflow-evidence-compression-v1"
REVIEWABLE_DISPOSITION = "reviewable"
SUPPRESSED_DISPOSITION = "suppressed_non_substantive"

_ASSISTANCE_STOP_WORDS = {
    "and", "are", "campaign", "content", "coverage", "current", "evidence", "for",
    "gap", "governed", "improve", "missing", "review", "safety", "source", "the",
    "this", "work", "workflow", "item", "required", "production", "windows",
    "authoring", "desktop", "prepare", "support",
}

_WORKFLOW_SOURCE_INTENT_ROOTS = (
    "connect", "configur", "creat", "diagnos", "enabl", "install", "recover",
    "repair", "reset", "resolv", "restor", "setup", "troubleshoot", "updat",
    "verif",
)


class KnowledgeEvidenceExtractionError(ValueError):
    pass


class _EvidenceParser(HTMLParser):
    """Small document parser that intentionally ignores page chrome."""

    BLOCKS = {"p", "li", "pre", "code"}
    HEADINGS = {"h1", "h2", "h3", "h4"}
    IGNORED = {"script", "style", "nav", "footer", "header", "form", "aside", "svg"}
    NON_CONTENT_HEADINGS = {
        "feedback", "relatedlinks", "relatedtopics", "commonparameters",
        "moresupportoptions", "seealsorecommendedcontent",
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
        self.main_depth = 0
        self.main_seen = False
        self.block_main = False

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attrs = {str(key).casefold(): str(value or "").casefold() for key, value in attrs}
        structural_tokens = f"{attrs.get('id', '')} {attrs.get('class', '')} {attrs.get('role', '')}"
        if any(token in structural_tokens for token in
               ("breadcrumb", "table-of-contents", "toc", "page-metadata", "navigation",
                "related", "recommend", "sidebar", "privacy", "consent", "cookie",
                "social-share", "support-options")):
            self.structural_depth += 1
            self.structural_tags.append(tag)
        if tag in {"main", "article"}:
            self.main_depth += 1
            self.main_seen = True
        if tag in self.IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCKS | self.HEADINGS:
            self.active_tag, self.parts = tag, []
            self.link_characters, self.link_count = 0, 0
            self.block_structural = bool(self.structural_depth)
            self.block_main = bool(self.main_depth)
        elif not self.ignored_depth and tag == "a" and self.active_tag:
            self.link_depth += 1
            self.link_count += 1

    def handle_endtag(self, tag):
        tag = tag.casefold()
        closes_structural = bool(self.structural_tags and tag == self.structural_tags[-1])
        closes_main = tag in {"main", "article"} and self.main_depth
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
            if closes_main:
                self.main_depth -= 1
            return
        text = " ".join(" ".join(self.parts).split())
        if tag in self.HEADINGS:
            self.heading = text[:180]
        elif len(text) >= 24 or (self.block_main and len(text) >= 12):
            heading_key = re.sub(r"[^a-z0-9]+", " ", self.heading.casefold()).strip()
            text_key = " ".join(text.casefold().split())
            if (heading_key.replace(" ", "") not in self.NON_CONTENT_HEADINGS and
                    not any(marker in text_key for marker in self.NON_CONTENT_TEXT)):
                self.blocks.append({"tag": tag, "heading": self.heading, "text": text,
                                    "link_characters": self.link_characters,
                                    "link_count": self.link_count,
                                    "structural_context": self.block_structural,
                                    "main_content": self.block_main})
        self.active_tag, self.parts = None, []
        if closes_structural:
            self.structural_tags.pop()
            self.structural_depth -= 1
        if closes_main:
            self.main_depth -= 1

    def handle_data(self, data):
        if not self.ignored_depth and self.active_tag:
            self.parts.append(data)
            if self.link_depth:
                self.link_characters += len(data.strip())


class KnowledgeEvidenceExtractionService:
    """Human-gated extraction from one already-approved research candidate."""

    MAX_PASSAGE = 420
    MAX_UNITS = 80
    MAX_WORKFLOW_REVIEWABLE_PROPOSITIONS = 24
    EXTRACTION_METHOD = "deterministic-html-block-v4"

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 policy_path: Path | None = None,
                 taxonomy_path: Path | None = None,
                 http_validator: SourceHTTPValidator | None = None):
        self.repository_root = resolve_data_root(repository_root, legacy_root=Path(__file__).resolve().parents[2])
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
        content_policy_stale = bool(
            units and (package.get("retrieval") or {}).get("source_content_policy")
            != SOURCE_CONTENT_POLICY
        )
        workflow_propositions_required = self._requires_exact_topic_relevance(
            self._governed_context(package)
        )
        proposition_policy_stale = bool(
            units and workflow_propositions_required and
            (package.get("retrieval") or {}).get("workflow_proposition_policy")
            != WORKFLOW_PROPOSITION_POLICY
        )
        compression_policy_stale = bool(
            units and workflow_propositions_required and
            (package.get("retrieval") or {}).get("workflow_evidence_compression_policy")
            != WORKFLOW_EVIDENCE_COMPRESSION_POLICY
        )
        source_stale = package.get("status") == "needs_refresh"
        reasons = [
            reason for reason, active in (
                ("extractor_version", version_stale),
                ("source_content_window", content_policy_stale),
                ("workflow_proposition_policy", proposition_policy_stale),
                ("workflow_evidence_compression_policy", compression_policy_stale),
                ("source_refresh", source_stale),
            ) if active
        ]
        return {
            "available": (
                version_stale or content_policy_stale or proposition_policy_stale or
                compression_policy_stale or source_stale
            ),
            "reason": "extractor_version" if version_stale else (
                "source_content_window" if content_policy_stale else (
                    "workflow_proposition_policy" if proposition_policy_stale else (
                        "workflow_evidence_compression_policy" if compression_policy_stale else (
                            "source_refresh" if source_stale else None
                        )
                    )
                )
            ),
            "package_methods": methods,
            "package_method_label": ", ".join(methods) if methods else "not extracted",
            "current_method": self.EXTRACTION_METHOD,
            "reasons": reasons,
            "compression_policy_adoption_required": compression_policy_stale,
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
            "source_title": sanitize_source_title(candidate.get("page_title"))
            or "Authoritative source",
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
        if research.get("research_objective") is not None:
            package["research_objective"] = deepcopy(
                research["research_objective"]
            )
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
            context = self._governed_context(package)
            workflow_propositions_required = self._requires_exact_topic_relevance(context)
            methods = {unit.get("extraction_method") for unit in package.get("evidence_units") or []}
            if (package.get("source_fingerprint") == fingerprint and
                    package.get("evidence_units") and methods == {self.EXTRACTION_METHOD} and
                    (package.get("retrieval") or {}).get("source_content_policy")
                    == SOURCE_CONTENT_POLICY and
                    (not workflow_propositions_required or
                     (package.get("retrieval") or {}).get("workflow_proposition_policy")
                     == WORKFLOW_PROPOSITION_POLICY) and
                    (not workflow_propositions_required or
                     (package.get("retrieval") or {}).get("workflow_evidence_compression_policy")
                     == WORKFLOW_EVIDENCE_COMPRESSION_POLICY)):
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
            source_title = sanitize_source_title(
                inspected.get("page_title") or package.get("source_title")
            ) or "Authoritative source"
            package["source_title"] = source_title
            units = self._extract_units(package, inspected.get("content_preview", ""), final_url)
            context = self._governed_context(package)
            for unit in units:
                unit["candidacy"] = self.candidacy_recommendation(unit, context)
            package["retrieval"] = {
                "requested_url": package["canonical_source_url"], "resolved_url": final_url,
                "http_status": inspected.get("http_status"), "retrieved_at": now,
                "content_type": inspected.get("content_type"),
                "source_title": source_title,
                "publisher": package.get("publisher"), "source_fingerprint": fingerprint,
                "redirect_chain": inspected.get("redirect_chain") or [], "result": "retrieved",
                "last_modified": inspected.get("last_modified"), "etag": inspected.get("etag"),
                "source_content_policy": SOURCE_CONTENT_POLICY,
            }
            if workflow_propositions_required:
                package["retrieval"]["workflow_proposition_policy"] = (
                    WORKFLOW_PROPOSITION_POLICY
                )
                package["retrieval"]["workflow_evidence_compression_policy"] = (
                    WORKFLOW_EVIDENCE_COMPRESSION_POLICY
                )
            package["source_fingerprint"] = fingerprint
            package["evidence_units"] = units
            package["candidacy"] = self._empty_candidacy_state()
            package["extracted_at"] = now
            package["updated_at"] = now
            if workflow_propositions_required:
                self._compress_workflow_evidence(package, context, now)
            else:
                package["status"] = "needs_review"
            event = "evidence_reextracted" if is_reextraction else "evidence_extracted"
            self._event(package, event, now, actor="Deterministic Extractor",
                        evidence_count=len(units), source_fingerprint=fingerprint,
                        extraction_method=self.EXTRACTION_METHOD,
                        reviewable_count=sum(self._is_reviewable(unit) for unit in units),
                        suppressed_count=sum(not self._is_reviewable(unit) for unit in units),
                        content_disposition_rule_version=CONTENT_DISPOSITION_RULE_VERSION,
                        source_content_policy=SOURCE_CONTENT_POLICY,
                        workflow_proposition_policy=(
                            WORKFLOW_PROPOSITION_POLICY
                            if workflow_propositions_required else None
                        ),
                        workflow_evidence_compression_policy=(
                            WORKFLOW_EVIDENCE_COMPRESSION_POLICY
                            if workflow_propositions_required else None
                        ),
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

    def set_candidacy_role_group(
        self, extraction_id: str, members: list[dict[str, Any]], role: str, *,
        group_id: str, group_fingerprint: str, actor: str = "Human",
    ) -> dict[str, Any]:
        """Atomically apply one role decision to an exact compatible evidence set."""
        if role not in CANDIDACY_ROLES:
            raise KnowledgeEvidenceExtractionError("Unknown candidacy role.")
        package = self.get(extraction_id)
        decision = f"role:{role}"
        if self._group_decision_recorded(
            package, group_id, group_fingerprint, decision
        ):
            return package
        units = self._validate_group_members(package, members)
        if all((unit.get("candidacy") or {}).get("human_confirmed_role") == role
               for unit in units):
            return package
        for unit in units:
            previous = (unit.get("candidacy") or {}).get("human_confirmed_role")
            if previous is not None:
                raise KnowledgeEvidenceExtractionError(
                    "An evidence role changed before the grouped decision. Nothing was saved."
                )
            if (previous == "candidate" and unit.get("review_state") != "proposed"
                    and role == "context"):
                raise KnowledgeEvidenceExtractionError(
                    "Reviewed evidence cannot be reinterpreted as Reviewer Context."
                )
        now = self._now()
        for unit in units:
            unit.setdefault("candidacy", self.candidacy_recommendation(
                unit, self._governed_context(package)
            )).update(
                human_confirmed_role=role,
                role_decided_at=now,
                role_decided_by=actor,
            )
            self._event(
                package, "candidacy_role_changed", now, actor=actor,
                evidence_id=unit["evidence_id"], previous_role=None,
                role=role, builder_group_id=group_id,
            )
        state = package.setdefault("candidacy", self._empty_candidacy_state())
        if state.get("candidate_set_status") == "confirmed":
            state.update(
                candidate_set_status="stale", stale_at=now,
                stale_reason="human_role_changed",
            )
        unresolved = [
            unit for unit in package.get("evidence_units") or []
            if self._is_reviewable(unit)
            and (unit.get("candidacy") or {}).get("human_confirmed_role")
            not in CANDIDACY_ROLES
        ]
        if not unresolved:
            rule_version = state.get("rule_version") or CANDIDACY_RULE_VERSION
            fingerprint = self._candidate_set_fingerprint(
                package, rule_version=rule_version
            )
            candidate_count = sum(
                (unit.get("candidacy") or {}).get("human_confirmed_role") == "candidate"
                for unit in package["evidence_units"]
            )
            context_count = sum(
                (unit.get("candidacy") or {}).get("human_confirmed_role") == "context"
                for unit in package["evidence_units"]
            )
            state.update({
                "schema_version": "1.0", "rule_version": rule_version,
                "candidate_set_status": "confirmed", "confirmed_at": now,
                "confirmed_by": actor, "confirmation_fingerprint": fingerprint,
                "candidate_set_outcome": (
                    "non_empty" if candidate_count else "empty"
                ),
            })
            state.pop("stale_at", None)
            state.pop("stale_reason", None)
            self._event(
                package, "candidate_set_confirmed", now, actor=actor,
                confirmation_fingerprint=fingerprint,
                candidate_set_outcome=state["candidate_set_outcome"],
                candidate_count=candidate_count, context_count=context_count,
                builder_group_id=group_id,
            )
        package["status"] = self._review_status(package["evidence_units"], package)
        package["updated_at"] = now
        self._event(
            package, "builder_evidence_group_decided", now, actor=actor,
            builder_group_id=group_id,
            builder_group_fingerprint=group_fingerprint,
            decision=decision,
            evidence_ids=[unit["evidence_id"] for unit in units],
        )
        self._save(package)
        return deepcopy(package)

    def review_evidence_group(
        self, extraction_id: str, members: list[dict[str, Any]],
        decision: str, notes: str = "", *, group_id: str,
        group_fingerprint: str, actor: str = "Human",
    ) -> dict[str, Any]:
        """Atomically review an exact set of current Candidate Evidence units."""
        if decision not in {"approved", "rejected", "needs_revision"}:
            raise KnowledgeEvidenceExtractionError("Unknown evidence review decision.")
        package = self.get(extraction_id)
        recorded_decision = f"evidence:{decision}"
        if self._group_decision_recorded(
            package, group_id, group_fingerprint, recorded_decision
        ):
            return package
        units = self._validate_group_members(package, members)
        if not self._candidate_set_current(package):
            raise KnowledgeEvidenceExtractionError(
                "The evidence candidate set changed. Nothing was saved."
            )
        notes = str(notes or "").strip()
        if all(unit.get("review_state") == decision
               and unit.get("reviewer_notes", "") == notes for unit in units):
            return package
        for unit in units:
            if ((unit.get("candidacy") or {}).get("human_confirmed_role")
                    != "candidate" or unit.get("review_state") != "proposed"):
                raise KnowledgeEvidenceExtractionError(
                    "A grouped Candidate Evidence decision changed. Nothing was saved."
                )
        now = self._now()
        for unit in units:
            unit.update(
                review_state=decision, reviewer_decision=decision,
                reviewer_notes=notes, reviewed_at=now, reviewed_by=actor,
            )
            self._event(
                package, f"evidence_{decision}", now, actor=actor,
                evidence_id=unit["evidence_id"], builder_group_id=group_id,
            )
        package["status"] = self._review_status(package["evidence_units"], package)
        package["updated_at"] = now
        self._event(
            package, "builder_evidence_group_decided", now, actor=actor,
            builder_group_id=group_id,
            builder_group_fingerprint=group_fingerprint,
            decision=recorded_decision,
            evidence_ids=[unit["evidence_id"] for unit in units],
        )
        self._save(package)
        return deepcopy(package)

    @staticmethod
    def _group_decision_recorded(package: dict[str, Any], group_id: str,
                                 group_fingerprint: str,
                                 decision: str) -> bool:
        return any(
            event.get("event") == "builder_evidence_group_decided"
            and event.get("builder_group_id") == group_id
            and event.get("builder_group_fingerprint") == group_fingerprint
            and event.get("decision") == decision
            for event in package.get("history") or []
        )

    def _validate_group_members(
        self, package: dict[str, Any], members: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not members or len(members) > 25:
            raise KnowledgeEvidenceExtractionError(
                "The grouped evidence set is empty or exceeds the governed limit."
            )
        expected = {str(item.get("evidence_id") or ""): item for item in members}
        if "" in expected or len(expected) != len(members):
            raise KnowledgeEvidenceExtractionError(
                "The grouped evidence identity is missing or ambiguous."
            )
        actual = {
            unit.get("evidence_id"): unit
            for unit in package.get("evidence_units") or []
            if unit.get("evidence_id") in expected
        }
        if len(actual) != len(expected):
            raise KnowledgeEvidenceExtractionError(
                "A grouped evidence member is no longer available. Nothing was saved."
            )
        result = []
        for evidence_id in sorted(expected):
            unit = actual[evidence_id]
            item = expected[evidence_id]
            candidacy = unit.get("candidacy") or {}
            if (
                not self._is_reviewable(unit)
                or unit.get("fingerprint") != item.get("evidence_fingerprint")
                or candidacy.get("recommendation_fingerprint")
                != item.get("recommendation_fingerprint")
                or candidacy.get("human_confirmed_role")
                != item.get("human_confirmed_role")
                or unit.get("review_state") != item.get("review_state")
            ):
                raise KnowledgeEvidenceExtractionError(
                    "A grouped evidence member changed. Nothing was saved."
                )
            result.append(unit)
        return result

    def _compress_workflow_evidence(self, package: dict[str, Any],
                                    context: dict[str, Any], now: str) -> None:
        """Apply the one allowlisted, deterministic missing-workflow settlement policy."""
        if not self._requires_exact_topic_relevance(context):
            return
        reviewable = [unit for unit in package.get("evidence_units") or []
                      if self._is_reviewable(unit)]
        conflicts = self._workflow_conflicting_evidence_ids(reviewable)
        counts = {"auto_context": 0, "auto_approved": 0, "human_exceptions": 0}
        for unit in reviewable:
            decision = self._workflow_compression_decision(unit, context, conflicts)
            coverage = self._workflow_coverage_roles(unit)
            unit["workflow_coverage_roles"] = coverage
            unit["workflow_evidence_compression"] = {
                "policy_id": WORKFLOW_EVIDENCE_COMPRESSION_POLICY,
                "decision": decision["decision"],
                "reason": decision["reason"],
                "decided_at": now,
                "evidence_fingerprint": unit.get("fingerprint"),
            }
            candidacy = unit.setdefault(
                "candidacy", self.candidacy_recommendation(unit, context)
            )
            if decision["decision"] == "auto_context":
                candidacy.update(human_confirmed_role="context", role_decided_at=now,
                                 role_decided_by="Deterministic Evidence Compression")
                counts["auto_context"] += 1
            elif decision["decision"] == "auto_approved":
                candidacy.update(human_confirmed_role="candidate", role_decided_at=now,
                                 role_decided_by="Deterministic Evidence Compression")
                unit.update(review_state="approved", reviewer_decision="approved",
                            reviewer_notes=("Direct, current, capability-relevant source evidence "
                                            "approved by the bounded workflow evidence policy."),
                            reviewed_at=now,
                            reviewed_by="Deterministic Evidence Compression")
                counts["auto_approved"] += 1
            else:
                counts["human_exceptions"] += 1

        state = package.setdefault("candidacy", self._empty_candidacy_state())
        state["workflow_evidence_compression_policy"] = WORKFLOW_EVIDENCE_COMPRESSION_POLICY
        state["compression_counts"] = counts
        if counts["human_exceptions"] == 0:
            fingerprint = self._candidate_set_fingerprint(package)
            candidate_count = counts["auto_approved"]
            state.update({
                "candidate_set_status": "confirmed", "confirmed_at": now,
                "confirmed_by": "Deterministic Evidence Compression",
                "confirmation_fingerprint": fingerprint,
                "candidate_set_outcome": "non_empty" if candidate_count else "empty",
            })
        package["status"] = self._review_status(package["evidence_units"], package)
        self._event(
            package, "workflow_evidence_compressed", now,
            actor="Deterministic Evidence Compression",
            policy_id=WORKFLOW_EVIDENCE_COMPRESSION_POLICY,
            **counts,
        )

    @classmethod
    def _workflow_compression_decision(cls, unit: dict[str, Any],
                                       context: dict[str, Any],
                                       conflicts: set[str]) -> dict[str, str]:
        evidence_id = str(unit.get("evidence_id") or "")
        evidence_type = str(unit.get("evidence_type") or "")
        text = str(unit.get("normalized_claim") or "")
        folded = text.casefold()
        assistance = cls.evidence_review_assistance(unit, context)
        coverage = cls._workflow_coverage_roles(unit)
        state_changing = bool(re.search(
            r"\b(restart|reset|install|uninstall|disable|enable|remove|repair|clear|"
            r"flush|renew|rollback|update|configure|delete|erase|format)\b", folded
        ))
        safety_sensitive = evidence_type in {"safety", "authorization_requirements", "commands"}
        uncertain_platform = assistance.get("category") == "review_attention"
        weak_relevance = not cls._topic_matches(
            {"heading": (unit.get("source_location") or {}).get("heading")}, text, context
        )
        if evidence_id in conflicts:
            return {"decision": "human_exception", "reason": "potentially_conflicting_evidence"}
        if state_changing or safety_sensitive:
            return {"decision": "human_exception", "reason": "unsafe_or_state_changing_content"}
        if uncertain_platform:
            return {"decision": "human_exception", "reason": "uncertain_platform_applicability"}
        if weak_relevance or unit.get("confidence") not in {"medium", "high"}:
            return {"decision": "human_exception", "reason": "weak_or_uncertain_relevance"}
        if evidence_type in {"procedure", "diagnostic_observations", "verification",
                             "expected_result", "alternate_outcomes", "escalation"} and coverage:
            return {"decision": "auto_approved", "reason": "direct_current_capability_evidence"}
        if (unit.get("candidacy") or {}).get("machine_recommended_role") == "undetermined":
            return {"decision": "human_exception", "reason": "ambiguous_evidence_role"}
        return {"decision": "auto_context", "reason": "high_confidence_reviewer_context"}

    @staticmethod
    def _workflow_coverage_roles(unit: dict[str, Any]) -> list[str]:
        evidence_type = str(unit.get("evidence_type") or "")
        text = str(unit.get("normalized_claim") or "").casefold()
        roles: list[str] = []
        if evidence_type in {"procedure", "diagnostic_observations"}:
            if re.search(r"\b(open|launch|start|go to|navigate|settings|setup)\b", text):
                roles.append("entry_setup")
            if re.search(r"\b(select|choose|connect|enter|click|run|perform|turn on)\b", text):
                roles.append("primary_action")
            if (re.search(r"\b(if|when|unless)\b", text) and
                    re.search(r"\b(prompt|sign[ -]?in|credential|password|username|enter|provide)\b", text)):
                roles.append("conditional_input")
        if evidence_type in {"verification", "expected_result"} or re.search(
                r"\b(verify|confirm|connected status|shows? connected|make sure)\b", text):
            roles.append("success_verification")
        if evidence_type == "alternate_outcomes":
            roles.append("branch_handling")
        if evidence_type == "escalation":
            roles.append("escalation")
        return list(dict.fromkeys(roles))

    @staticmethod
    def _workflow_conflicting_evidence_ids(units: list[dict[str, Any]]) -> set[str]:
        conflicts: set[str] = set()
        opposites = (("must ", "must not "), ("requires ", "does not require "),
                     ("supported", "not supported"), ("enable", "disable"))
        for index, left in enumerate(units):
            left_text = str(left.get("normalized_claim") or "").casefold()
            for right in units[index + 1:]:
                right_text = str(right.get("normalized_claim") or "").casefold()
                if any((a in left_text and b in right_text) or
                       (b in left_text and a in right_text) for a, b in opposites):
                    conflicts.update((str(left.get("evidence_id") or ""),
                                      str(right.get("evidence_id") or "")))
        return conflicts

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
        compression = self._workflow_compression_projection(
            package, decorated, suppressed_units, context
        )
        if compression.get("enabled") and not filters_active:
            filtered = compression["exception_units"]
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
            "compression": compression,
        }

    def _workflow_compression_projection(self, package: dict[str, Any],
                                         units: list[dict[str, Any]],
                                         suppressed: list[dict[str, Any]],
                                         context: dict[str, Any]) -> dict[str, Any]:
        enabled = (
            self._requires_exact_topic_relevance(context) and
            (package.get("retrieval") or {}).get("workflow_evidence_compression_policy")
            == WORKFLOW_EVIDENCE_COMPRESSION_POLICY
        )
        if not enabled:
            return {"enabled": False}
        exceptions = [
            unit for unit in units
            if (unit.get("workflow_evidence_compression") or {}).get("decision")
            == "human_exception"
        ]
        unresolved = [
            unit for unit in exceptions
            if unit.get("candidacy_role") == "unresolved" or
            (unit.get("candidacy_role") == "candidate" and
             unit.get("review_state") in {"proposed", "needs_revision"})
        ]
        approved_roles = {
            role for unit in units if unit.get("review_state") == "approved"
            and unit.get("candidacy_role") == "candidate"
            for role in unit.get("workflow_coverage_roles") or []
        }
        applicable_roles = {
            role for unit in units for role in unit.get("workflow_coverage_roles") or []
        }
        required_roles = {"entry_setup", "primary_action", "success_verification"}
        required_roles.update(applicable_roles & {"conditional_input", "branch_handling", "escalation"})
        return {
            "enabled": True,
            "policy_id": WORKFLOW_EVIDENCE_COMPRESSION_POLICY,
            "extracted_propositions": len(units),
            "suppressed": len(suppressed),
            "auto_context": sum(
                (unit.get("workflow_evidence_compression") or {}).get("decision") == "auto_context"
                for unit in units
            ),
            "auto_approved": sum(
                (unit.get("workflow_evidence_compression") or {}).get("decision") == "auto_approved"
                for unit in units
            ),
            "human_exceptions": len(exceptions),
            "remaining_exceptions": len(unresolved),
            "exception_units": unresolved,
            "settled_exception_units": [unit for unit in exceptions if unit not in unresolved],
            "settled_units": [unit for unit in units if unit not in unresolved],
            "procedure_coverage": {
                "required": sorted(required_roles),
                "covered": sorted(approved_roles & required_roles),
                "missing": sorted(required_roles - approved_roles),
            },
            "verification_coverage": "success_verification" in approved_roles,
            "ready_for_safe_processing": (
                not unresolved and package.get("status") == "approved"
            ),
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
            context = self._governed_context(package)
            for unit in package.get("evidence_units") or []:
                if (unit.get("review_state") != "approved" or
                        not self._is_reviewable(unit) or
                        (unit.get("candidacy") or {}).get("human_confirmed_role") != "candidate" or
                        unit["evidence_id"] in seen):
                    continue
                if (self._requires_exact_topic_relevance(context)
                        and not self._topic_matches(
                            {"heading": (unit.get("source_location") or {}).get("heading")},
                            unit.get("supporting_passage") or unit.get("normalized_claim") or "",
                            context,
                        )):
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
        context = self._governed_context(package)
        context["source_title"] = package.get("source_title") or ""
        blocks = parser.blocks
        if parser.main_seen and any(block.get("main_content") for block in blocks):
            blocks = [block for block in blocks if block.get("main_content")]
        if self._requires_exact_topic_relevance(context):
            blocks = self._consolidate_workflow_propositions(blocks, context)
        units, seen = [], set()
        for index, block in enumerate(blocks[: self.MAX_UNITS * 3]):
            text = block["text"][: self.MAX_PASSAGE].strip()
            normalized = re.sub(r"\s+", " ", text)
            key = normalized.casefold()
            if not normalized or key in seen:
                continue
            seen.add(key)
            evidence_type = self._classify(block["tag"], block["heading"], normalized)
            disposition = self._content_disposition(block, normalized, context)
            evidence_id = self._stable_id(
                "EVD", package["extraction_id"], evidence_type, normalized.casefold()
            )
            units.append({
                "evidence_id": evidence_id, "evidence_type": evidence_type,
                "normalized_claim": normalized, "supporting_passage": text,
                "source_location": {
                    "heading": block["heading"],
                    "block_index": block.get("source_index", index),
                    "block_indexes": [source_block["block_index"] for source_block in
                                      block.get("source_blocks") or []],
                    "html_element": block["tag"],
                },
                "source_url": source_url, "source_title": package["source_title"],
                "publisher": package.get("publisher"),
                "platform_applicability": package.get("platform") or "Unspecified",
                "confidence": "medium", "extraction_method": self.EXTRACTION_METHOD,
                "review_state": "proposed", "reviewer_decision": None,
                "reviewer_notes": "", "reviewed_at": None,
                "fingerprint": self._fingerprint({"text": normalized, "type": evidence_type}),
                "provenance": {"extraction_id": package["extraction_id"],
                               "research_package_id": package["research_package_id"],
                               "source_candidate_id": package["source_candidate_id"],
                               "source_blocks": deepcopy(block.get("source_blocks") or [{
                                   "block_index": block.get("source_index", index),
                                   "heading": block.get("heading") or "",
                                   "html_element": block.get("tag") or "",
                               }])},
                "content_disposition": block.get("content_disposition") or disposition,
            })
            if len(units) >= self.MAX_UNITS:
                break
        if not units:
            raise KnowledgeEvidenceExtractionError("No bounded technical evidence could be extracted safely.")
        if (self._requires_exact_topic_relevance(context) and
                sum(self._is_reviewable(unit) for unit in units)
                > self.MAX_WORKFLOW_REVIEWABLE_PROPOSITIONS):
            raise KnowledgeEvidenceExtractionError(
                "Source evidence remains too fragmented for bounded workflow review."
            )
        return units

    @classmethod
    def _consolidate_workflow_propositions(
            cls, blocks: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Group only adjacent, equivalent-context source blocks without paraphrasing."""
        prepared: list[dict[str, Any]] = []
        for source_index, original in enumerate(blocks):
            block = deepcopy(original)
            text = " ".join(str(block.get("text") or "").split()).strip()
            block["text"] = text
            block["source_index"] = source_index
            block["source_blocks"] = [{
                "block_index": source_index,
                "heading": block.get("heading") or "",
                "html_element": block.get("tag") or "",
                "text_fingerprint": cls._fingerprint(text),
            }]
            block["content_disposition"] = cls._content_disposition(block, text, context)
            prepared.append(block)

        consolidated: list[dict[str, Any]] = []
        for block in cls._deduplicate_exact_blocks(prepared):
            if not cls._block_is_reviewable(block):
                consolidated.append(block)
                continue

            previous = consolidated[-1] if consolidated else None
            if previous is not None and cls._blocks_form_one_proposition(previous, block):
                previous["text"] = f"{previous['text']} {block['text']}"
                previous["source_blocks"].extend(block["source_blocks"])
                continue
            consolidated.append(block)

        return cls._deduplicate_exact_blocks(consolidated)

    @classmethod
    def _deduplicate_exact_blocks(cls, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: list[dict[str, Any]] = []
        exact: dict[tuple[str, str], dict[str, Any]] = {}
        for block in blocks:
            if not cls._block_is_reviewable(block) or cls._is_branch_statement(block["text"]):
                deduplicated.append(block)
                continue
            key = (
                cls._effective_text_key(block.get("heading") or ""),
                cls._effective_text_key(block["text"]),
            )
            retained = exact.get(key)
            if retained is None:
                exact[key] = block
                deduplicated.append(block)
            else:
                retained["source_blocks"].extend(block["source_blocks"])
        return deduplicated

    @classmethod
    def _blocks_form_one_proposition(cls, previous: dict[str, Any], current: dict[str, Any]) -> bool:
        if not cls._block_is_reviewable(previous):
            return False
        if cls._effective_text_key(previous["text"]) == cls._effective_text_key(current["text"]):
            return False
        if cls._effective_text_key(previous.get("heading") or "") != cls._effective_text_key(
                current.get("heading") or ""):
            return False
        if cls._is_branch_statement(previous["text"]) or cls._is_branch_statement(current["text"]):
            return False
        if cls._classify(previous.get("tag") or "", previous.get("heading") or "",
                         previous["text"]) != cls._classify(
                             current.get("tag") or "", current.get("heading") or "",
                             current["text"]):
            return False
        return len(previous["text"]) + 1 + len(current["text"]) <= cls.MAX_PASSAGE

    @staticmethod
    def _block_is_reviewable(block: dict[str, Any]) -> bool:
        return (block.get("content_disposition") or {}).get("status") != SUPPRESSED_DISPOSITION

    @staticmethod
    def _is_branch_statement(text: str) -> bool:
        return bool(re.match(
            r"^(?:if\b|otherwise\b|unless\b|either\b|depending\b|when prompted\b|"
            r"do either\b|for .+?,\s*(?:choose|select|enter)\b)",
            str(text or "").strip(), flags=re.IGNORECASE,
        ))

    @staticmethod
    def _effective_text_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    @classmethod
    def _content_disposition(cls, block: dict[str, Any], text: str,
                             context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Classify only structurally or lexically provable non-substantive material."""
        normalized = " ".join(str(text or "").split()).strip()
        folded = normalized.casefold().rstrip(". ")
        reason = None
        basis = None
        heading_key = re.sub(
            r"[^a-z0-9]+", "", str(block.get("heading") or "").casefold()
        )
        if block.get("structural_context"):
            reason, basis = "source_navigation_container", "structural"
        elif heading_key in _EvidenceParser.NON_CONTENT_HEADINGS or any(
            marker in heading_key for marker in (
                "relatedtopics", "relatedcontent", "recommendedcontent",
                "moresupportoptions", "seemoretopics",
            )
        ):
            reason, basis = "related_topic_collection", "section_heading"
        elif re.search(
            r"\b(your privacy choices|opt[- ]out icon|privacy choices|cookie preferences)\b",
            folded,
        ):
            reason, basis = "privacy_control", "exact_text_pattern"
        elif (cls._requires_exact_topic_relevance(context or {})
              and len(normalized) <= 80 and normalized.endswith(":")):
            reason, basis = "instruction_lead_in_fragment", "fragment_shape"
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
        elif (cls._requires_exact_topic_relevance(context or {})
              and not cls._topic_matches(block, normalized, context or {})):
            reason, basis = "unrelated_to_governed_work_item", "governed_topic_terms"
        elif (cls._requires_exact_topic_relevance(context or {})
              and not cls._matches_source_intent(block, normalized, context or {})):
            reason, basis = "outside_approved_source_intent", "source_title_intent"
        elif (cls._is_verification_recovery(context or {})
              and not cls._matches_verification_objective(block, normalized)):
            reason, basis = (
                "outside_verification_recovery_objective",
                "governed_research_objective",
            )
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
    def _requires_exact_topic_relevance(context: dict[str, Any]) -> bool:
        return (
            str(context.get("gap_type") or "").casefold() == "missing_workflow"
            and str(context.get("work_type") or "").casefold() == "workflow"
            and bool(context.get("capability_id"))
        )

    @staticmethod
    def _is_verification_recovery(context: dict[str, Any]) -> bool:
        return ((context.get("research_objective") or {}).get("kind")
                == "workflow_success_verification")

    @staticmethod
    def _matches_verification_objective(
        block: dict[str, Any], text: str,
    ) -> bool:
        value = " ".join((
            str(block.get("heading") or ""), str(text or "")
        )).casefold()
        return bool(re.search(
            r"\b(verify|verification|confirm(?:ed|ing)?|expected result|"
            r"successful(?:ly)?|success|status|working|resolved|result|"
            r"appears|shows?|displays?|listed|recognized|detected)\b",
            value,
        ))

    @classmethod
    def _topic_matches(cls, block: dict[str, Any], text: str,
                       context: dict[str, Any]) -> bool:
        haystack = " ".join((str(block.get("heading") or ""), str(text or ""))).casefold()
        terms = [str(term).casefold().strip() for term in context.get("topic_terms") or []]
        if not terms:
            terms = cls._assistance_terms(context)
        return any(
            term and re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack)
            for term in terms
        )

    @staticmethod
    def _matches_source_intent(block: dict[str, Any], text: str,
                               context: dict[str, Any]) -> bool:
        title = str(context.get("source_title") or "").casefold()
        intent_roots = {
            root for root in _WORKFLOW_SOURCE_INTENT_ROOTS if root in title
        }
        if not intent_roots:
            return True
        heading = str(block.get("heading") or "").casefold()
        heading_key = re.sub(r"[^a-z0-9]+", "", heading)
        target = text.casefold() if heading_key in {"summary", "overview"} else heading
        return any(root in target for root in intent_roots)

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
        if (package is not None and
                (package.get("candidacy") or {}).get("workflow_evidence_compression_policy") and
                "needs_revision" in states):
            return "needs_review"
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
            planner = self.research.planner
            research = self.research.get(
                str(package.get("research_package_id") or "")
            )
            persisted_objective = package.get("research_objective")
            current_objective = research.get("research_objective")
            if persisted_objective != current_objective:
                raise KnowledgeEvidenceExtractionError(
                    "The governed research objective changed before evidence extraction."
                )
            campaign = planner.get(str(package.get("campaign_id") or ""))
            work = next((x for x in campaign.get("work_items", [])
                         if x.get("work_item_id") == package.get("work_item_id")), {})
            gap = next((x for x in campaign.get("gaps", [])
                        if x.get("gap_id") == package.get("gap_id")), {})
            capability_id = str(
                work.get("capability_id") or gap.get("capability_id") or ""
            ).strip()
            capabilities = [
                capability
                for domain in planner.domains()
                if domain.get("id") == campaign.get("domain")
                for capability in domain.get("areas") or []
                if capability.get("id") == capability_id
            ]
            topic_terms = list(capabilities[0].get("terms") or []) if len(capabilities) == 1 else []
            if len(capabilities) == 1:
                topic_terms.extend((capabilities[0].get("id"), capabilities[0].get("title")))
            context.update(campaign_title=campaign.get("title") or "Campaign",
                           campaign_objective=campaign.get("objective") or "",
                           area=work.get("area_id") or gap.get("area") or campaign.get("scope") or "Not specified",
                           platform=work.get("platform") or package.get("platform") or "",
                           work_type=work.get("work_type") or "Not specified",
                           gap_type=gap.get("gap_type") or "Not specified",
                           facet=(gap.get("facet") or work.get("facet") or
                                  work.get("objective_facet") or ""),
                           gap_summary=gap.get("summary") or work.get("reason") or work.get("title") or context["gap_summary"],
                           capability_id=capability_id,
                           topic_terms=list(dict.fromkeys(
                               str(term).strip() for term in topic_terms if str(term).strip()
                           )),
                           research_objective=deepcopy(current_objective))
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
        compression_current = True
        if (state.get("workflow_evidence_compression_policy") or
                (package.get("retrieval") or {}).get("workflow_evidence_compression_policy")):
            compression_current = (
                state.get("workflow_evidence_compression_policy")
                == WORKFLOW_EVIDENCE_COMPRESSION_POLICY
                and (package.get("retrieval") or {}).get("source_fingerprint")
                == package.get("source_fingerprint")
                and all(self._workflow_compressed_unit_current(unit)
                        for unit in package.get("evidence_units") or []
                        if self._is_reviewable(unit))
            )
        return (state.get("candidate_set_status") == "confirmed" and
                rule_version in {CANDIDACY_RULE_VERSION, *LEGACY_CANDIDACY_RULE_VERSIONS} and
                state.get("confirmation_fingerprint") == self._candidate_set_fingerprint(
                    package, rule_version=rule_version) and compression_current)

    @classmethod
    def _workflow_compressed_unit_current(cls, unit: dict[str, Any]) -> bool:
        compression = unit.get("workflow_evidence_compression") or {}
        if compression.get("policy_id") != WORKFLOW_EVIDENCE_COMPRESSION_POLICY:
            return False
        expected = cls._fingerprint({
            "text": str(unit.get("normalized_claim") or "").strip(),
            "type": unit.get("evidence_type"),
        })
        return (unit.get("fingerprint") == expected and
                compression.get("evidence_fingerprint") == expected)

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
