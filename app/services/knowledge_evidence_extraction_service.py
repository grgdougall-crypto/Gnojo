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


EXTRACTION_STATUSES = (
    "proposed", "retrieving", "extracted", "needs_review", "partially_approved",
    "approved", "needs_refresh", "failed", "rejected", "superseded",
)
EVIDENCE_REVIEW_STATES = ("proposed", "approved", "rejected", "needs_revision")


class KnowledgeEvidenceExtractionError(ValueError):
    pass


class _EvidenceParser(HTMLParser):
    """Small document parser that intentionally ignores page chrome."""

    BLOCKS = {"p", "li", "pre", "code"}
    HEADINGS = {"h1", "h2", "h3", "h4"}
    IGNORED = {"script", "style", "nav", "footer", "header", "form", "aside", "svg"}

    def __init__(self):
        super().__init__()
        self.ignored_depth = 0
        self.active_tag: str | None = None
        self.parts: list[str] = []
        self.heading = ""
        self.blocks: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self.IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCKS | self.HEADINGS:
            self.active_tag, self.parts = tag, []

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self.IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth or tag != self.active_tag:
            return
        text = " ".join(" ".join(self.parts).split())
        if tag in self.HEADINGS:
            self.heading = text[:180]
        elif len(text) >= 24:
            self.blocks.append({"tag": tag, "heading": self.heading, "text": text})
        self.active_tag, self.parts = None, []

    def handle_data(self, data):
        if not self.ignored_depth and self.active_tag:
            self.parts.append(data)


class KnowledgeEvidenceExtractionService:
    """Human-gated extraction from one already-approved research candidate."""

    MAX_PASSAGE = 420
    MAX_UNITS = 80

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
            if package.get("source_fingerprint") == fingerprint and package.get("evidence_units"):
                return deepcopy(package)
            package["status"] = "retrieving"
            self._event(package, "retrieval_started", now, actor="Human")
            self._save(package)
            if package.get("source_fingerprint") and package.get("evidence_units"):
                package["evidence_revisions"].append({
                    "source_fingerprint": package["source_fingerprint"],
                    "evidence_units": deepcopy(package["evidence_units"]),
                    "superseded_at": now,
                })
            units = self._extract_units(package, inspected.get("content_preview", ""), final_url)
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
            package["extracted_at"] = now
            package["updated_at"] = now
            package["status"] = "needs_review"
            self._event(package, "evidence_extracted", now, actor="Deterministic Extractor",
                        evidence_count=len(units), source_fingerprint=fingerprint)
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
        notes = str(notes or "").strip()
        if unit.get("review_state") == decision and unit.get("reviewer_notes", "") == notes:
            return package
        now = self._now()
        unit["review_state"] = decision
        unit["reviewer_decision"] = decision
        unit["reviewer_notes"] = notes
        unit["reviewed_at"] = now
        package["status"] = self._review_status(package["evidence_units"])
        package["updated_at"] = now
        self._event(package, f"evidence_{decision}", now, actor="Human", evidence_id=evidence_id)
        self._save(package)
        return deepcopy(package)

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
            if package.get("status") not in {"approved", "partially_approved"}:
                continue
            for unit in package.get("evidence_units") or []:
                if unit.get("review_state") != "approved" or unit["evidence_id"] in seen:
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
                "confidence": "medium", "extraction_method": "deterministic-html-block-v1",
                "review_state": "proposed", "reviewer_decision": None,
                "reviewer_notes": "", "reviewed_at": None,
                "fingerprint": self._fingerprint({"text": normalized, "type": evidence_type}),
                "provenance": {"extraction_id": package["extraction_id"],
                               "research_package_id": package["research_package_id"],
                               "source_candidate_id": package["source_candidate_id"]},
            })
            if len(units) >= self.MAX_UNITS:
                break
        if not units:
            raise KnowledgeEvidenceExtractionError("No bounded technical evidence could be extracted safely.")
        return units

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

    @staticmethod
    def _review_status(units: list[dict[str, Any]]) -> str:
        states = [unit.get("review_state", "proposed") for unit in units]
        if states and all(state == "approved" for state in states):
            return "approved"
        if any(state == "approved" for state in states):
            return "partially_approved"
        return "needs_review"

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
