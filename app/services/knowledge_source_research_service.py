from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from curator.inventory import CuratorInventory
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)


RESEARCH_STATUSES = (
    "pending", "researching", "ready_for_review", "approved", "rejected",
    "needs_refresh", "archived",
)
CANDIDATE_STATES = ("proposed", "selected", "rejected")
TRACKING_PARAMETERS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
}
DEAD_PAGE_MARKERS = (
    "sorry, page not found", "404 - page not found", "404 not found",
    "the chosen document is not currently available",
    "this document is not currently available",
    "the requested document is not available",
)


class KnowledgeSourceResearchError(ValueError):
    pass


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._inside = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "title":
            self._inside = True

    def handle_endtag(self, tag):
        if tag.casefold() == "title":
            self._inside = False

    def handle_data(self, data):
        if self._inside:
            self.parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.parts).split())


class SourceAuthorityPolicy:
    """Configurable authority and vendor-target policy, separate from research."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeSourceResearchError(f"Unable to read source authority policy: {error}") from error
        if self.value.get("schema_version") != "1.0":
            raise KnowledgeSourceResearchError("Unsupported source authority policy.")

    def classify(self, url: str) -> dict[str, Any]:
        domain = (urlsplit(url).hostname or "").casefold()
        for tier in self.value.get("tiers", []):
            for publisher in tier.get("publishers", []):
                if any(self._domain_matches(domain, allowed) for allowed in publisher.get("domains", [])):
                    return {
                        "authority_tier": int(tier["tier"]),
                        "authority_label": tier["label"],
                        "publisher": publisher["name"],
                    }
        return {"authority_tier": None, "authority_label": "Unclassified", "publisher": None}

    def target(self, platform: str, vendor: str | None = None) -> dict[str, Any] | None:
        for target in self.value.get("research_targets", []):
            if target.get("platform", "").casefold() != platform.casefold():
                continue
            if vendor and target.get("vendor", "").casefold() != vendor.casefold():
                continue
            return deepcopy(target)
        return None

    @staticmethod
    def _domain_matches(domain: str, allowed: str) -> bool:
        allowed = allowed.casefold().lstrip(".")
        return domain == allowed or domain.endswith(f".{allowed}")


class MicrosoftLearnSearchProvider:
    """Narrow official-documentation search adapter for the Windows pilot."""

    ENDPOINT = "https://learn.microsoft.com/api/search"

    def __init__(self, session=None):
        self.session = session or requests

    def search(self, query: str, *, domains: list[str], limit: int = 8) -> list[dict[str, Any]]:
        response = self.session.get(
            self.ENDPOINT,
            params={"search": query, "locale": "en-us", "$top": min(limit, 10)},
            timeout=10,
            headers={"User-Agent": "Gnojo-Knowledge-Research/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", []) if isinstance(payload, dict) else []
        return [{
            "title": item.get("title"), "url": item.get("url"),
            "summary": item.get("description") or item.get("summary") or "",
            "publisher": "Microsoft", "last_updated": item.get("lastUpdatedDate"),
        } for item in results[:limit] if isinstance(item, dict)]


class SourceHTTPValidator:
    """Bounded, redirect-aware HTTPS validation with basic SSRF protection."""

    def __init__(self, session=None, host_resolver=None):
        self.session = session or requests
        self.host_resolver = host_resolver or socket.getaddrinfo

    def inspect(self, url: str) -> dict[str, Any]:
        current = _resolution_url(url)
        redirects: list[str] = []
        for _ in range(6):
            self._validate_destination(current)
            response = self.session.get(
                current, allow_redirects=False, timeout=10, stream=True,
                headers={"User-Agent": "Gnojo-Knowledge-Research/1.0"},
            )
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise KnowledgeSourceResearchError("Source redirect omitted its destination.")
                    current = _resolution_url(urljoin(current, location))
                    redirects.append(current)
                    continue
                if status < 200 or status >= 400:
                    raise KnowledgeSourceResearchError(f"Source returned HTTP {status}.")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
                if content_type and content_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
                    raise KnowledgeSourceResearchError(f"Unsupported source content type '{content_type}'.")
                preview = b""
                for chunk in response.iter_content(chunk_size=8192):
                    preview += chunk
                    if len(preview) >= 262144:
                        preview = preview[:262144]
                        break
                text = preview.decode("utf-8", errors="ignore")
                if any(marker in text.casefold() for marker in DEAD_PAGE_MARKERS):
                    raise KnowledgeSourceResearchError("Source resolved to an unavailable-page response.")
                parser = _TitleParser()
                if content_type != "application/pdf":
                    parser.feed(text)
                return {
                    "http_status": status,
                    "final_url": current,
                    "redirect_chain": redirects,
                    "page_title": parser.title,
                    "content_type": content_type or "unknown",
                    "last_modified": response.headers.get("Last-Modified"),
                    "etag": response.headers.get("ETag"),
                    "content_digest": hashlib.sha256(preview).hexdigest(),
                    "content_preview": text[:65536],
                }
            finally:
                response.close()
        raise KnowledgeSourceResearchError("Source exceeded the redirect limit.")

    def _validate_destination(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise KnowledgeSourceResearchError("Research sources must use a public HTTPS URL.")
        if parsed.port not in (None, 443):
            raise KnowledgeSourceResearchError("Research sources must use the standard HTTPS port.")
        try:
            addresses = self.host_resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError as error:
            raise KnowledgeSourceResearchError("Source host could not be resolved.") from error
        if not addresses:
            raise KnowledgeSourceResearchError("Source host could not be resolved.")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise KnowledgeSourceResearchError("Source host does not resolve to a public address.")


def canonicalize_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise KnowledgeSourceResearchError("A valid HTTPS source URL is required.")
    host = parsed.hostname.casefold()
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMETERS
    ))
    return urlunsplit(("https", f"{host}{port}", path, query, ""))


def _resolution_url(url: str) -> str:
    """Canonicalize a request URL without removing a server-significant trailing slash."""
    canonical = canonicalize_url(url)
    original = urlsplit(str(url or "").strip())
    parsed = urlsplit(canonical)
    if original.path.endswith("/") and original.path != "/" and not parsed.path.endswith("/"):
        return urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path}/", parsed.query, ""))
    return canonical


class KnowledgeSourceResearchService:
    """Supervised campaign research that never mutates authoritative content."""

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 policy_path: Path | None = None,
                 search_providers: dict[str, Any] | None = None,
                 http_validator: SourceHTTPValidator | None = None,
                 taxonomy_path: Path | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.package_root = self.campaign_root / "research"
        self.policy = SourceAuthorityPolicy(policy_path or self.repository_root / "app" / "data" / "source_authority_policy.json")
        self.search_providers = search_providers or {"microsoft_learn": MicrosoftLearnSearchProvider()}
        self.http_validator = http_validator or SourceHTTPValidator()
        self.planner = KnowledgeCoveragePlannerService(
            self.repository_root, self.campaign_root,
            taxonomy_path or self.repository_root / "app" / "data" / "knowledge_coverage_taxonomy.json",
        )

    def list_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        packages = [self._read(path) for path in self.package_root.glob("KRP-*.json")]
        return sorted((item for item in packages if item["campaign_id"] == campaign_id),
                      key=lambda item: item["created_at"], reverse=True)

    def create(self, campaign_id: str, gap_id: str, work_item_id: str,
               requested_evidence_type: str = "authoritative_source") -> dict[str, Any]:
        campaign, gap, work_item = self._context(campaign_id, gap_id, work_item_id)
        package_id = self._stable_id("KRP", campaign_id, gap_id, work_item_id)
        path = self._path(package_id)
        if path.exists():
            return self._read(path)
        platform = str((campaign.get("platforms") or [""])[0])
        vendor = "Microsoft" if platform.casefold() == "windows" else None
        now = self._now()
        package = {
            "schema_version": "1.0", "package_id": package_id,
            "campaign_id": campaign_id, "gap_id": gap_id, "work_item_id": work_item_id,
            "target_coverage_area": gap["area_id"], "coverage_facet": gap["facet"],
            "requested_evidence_type": requested_evidence_type,
            "platform": platform, "product_vendor": vendor,
            "status": "pending", "created_at": now, "last_checked_at": None,
            "research_query": None, "existing_sources": [], "candidate_sources": [],
            "selected_sources": [], "rejected_sources": [], "reuse_recommendation": None,
            "remaining_gaps": [], "research_notes": "",
            "history": [{"event": "created", "at": now, "actor": "Human"}],
        }
        self._save(package)
        self._attach_reference(campaign, package)
        return deepcopy(package)

    def get(self, package_id: str) -> dict[str, Any]:
        path = self._path(package_id)
        if not path.exists():
            raise KnowledgeSourceResearchError(f"Research package '{package_id}' was not found.")
        return self._read(path)

    def run(self, package_id: str, *, force_external: bool = False) -> dict[str, Any]:
        package = self.get(package_id)
        if package.get("last_checked_at") and not force_external and package.get("status") == "ready_for_review":
            return package
        campaign, gap, work_item = self._context(package["campaign_id"], package["gap_id"], package["work_item_id"])
        package["status"] = "researching"
        existing = self._existing_sources(package, gap, campaign)
        package["existing_sources"] = existing
        adequate = [item for item in existing if item.get("authority_tier") in {1, 2} and item.get("http_status") == 200]
        if adequate and not force_external:
            package["candidate_sources"] = adequate
            package["reuse_recommendation"] = {
                "recommended": True,
                "reason": "Existing authoritative Gnojo evidence supports this coverage area; external discovery was skipped.",
                "source_candidate_ids": [item["source_candidate_id"] for item in adequate],
            }
            package["research_query"] = None
        else:
            package["reuse_recommendation"] = None
            try:
                query, discovered = self._external_candidates(package, gap, campaign)
                package["research_query"] = query
                package["candidate_sources"] = self._merge_candidates(existing, discovered)
            except KnowledgeSourceResearchError as error:
                package["status"] = "pending"
                package["research_notes"] = str(error)
                package["history"].append({"event": "research_failed", "at": self._now(),
                                           "actor": "Research Service", "reason": str(error)})
                self._save(package)
                self._sync_reference(package)
                raise
        package["last_checked_at"] = self._now()
        package["status"] = "ready_for_review"
        package["selected_sources"] = [item["source_candidate_id"] for item in package["candidate_sources"]
                                       if item.get("review_state") == "selected"]
        package["rejected_sources"] = [item["source_candidate_id"] for item in package["candidate_sources"]
                                       if item.get("review_state") == "rejected"]
        package["remaining_gaps"] = ([] if package["candidate_sources"] else [
            "No resolving, topic-relevant authoritative candidate is currently available."
        ])
        fingerprint = self._fingerprint({
            "existing": package["existing_sources"], "candidates": package["candidate_sources"],
            "reuse": package["reuse_recommendation"], "remaining": package["remaining_gaps"],
        })
        if not package.get("history") or package["history"][-1].get("fingerprint") != fingerprint:
            package["history"].append({
                "event": "researched", "at": package["last_checked_at"], "actor": "Research Service",
                "fingerprint": fingerprint, "external_research": bool(package["research_query"]),
            })
        self._save(package)
        self._sync_reference(package)
        return deepcopy(package)

    def set_candidate_state(self, package_id: str, candidate_id: str, state: str,
                            notes: str = "") -> dict[str, Any]:
        if state not in {"selected", "rejected"}:
            raise KnowledgeSourceResearchError("Candidate must be selected or rejected by a reviewer.")
        package = self.get(package_id)
        candidate = next((item for item in package["candidate_sources"]
                          if item["source_candidate_id"] == candidate_id), None)
        if candidate is None:
            raise KnowledgeSourceResearchError("Source candidate was not found in this package.")
        candidate["review_state"] = state
        candidate["reviewer_notes"] = str(notes or "").strip()
        package["selected_sources"] = [item["source_candidate_id"] for item in package["candidate_sources"]
                                       if item["review_state"] == "selected"]
        package["rejected_sources"] = [item["source_candidate_id"] for item in package["candidate_sources"]
                                       if item["review_state"] == "rejected"]
        package["history"].append({"event": f"candidate_{state}", "at": self._now(), "actor": "Human",
                                   "source_candidate_id": candidate_id})
        self._save(package)
        self._sync_reference(package)
        return deepcopy(package)

    def review(self, package_id: str, status: str, notes: str = "") -> dict[str, Any]:
        if status not in {"approved", "rejected", "needs_refresh", "archived"}:
            raise KnowledgeSourceResearchError("Unknown research review decision.")
        package = self.get(package_id)
        if status == "approved" and not package.get("selected_sources"):
            raise KnowledgeSourceResearchError("Select at least one source before approving the research package.")
        package["status"] = status
        package["research_notes"] = str(notes or "").strip()
        package["history"].append({"event": status, "at": self._now(), "actor": "Human"})
        self._save(package)
        self._sync_reference(package)
        return deepcopy(package)

    def refresh_candidate(self, package_id: str, candidate_id: str) -> dict[str, Any]:
        package = self.get(package_id)
        index = next((i for i, item in enumerate(package["candidate_sources"])
                      if item["source_candidate_id"] == candidate_id), None)
        if index is None:
            raise KnowledgeSourceResearchError("Source candidate was not found in this package.")
        current = package["candidate_sources"][index]
        refreshed = self._validate_candidate(package, {
            "title": current["page_title"], "url": current["canonical_url"],
            "summary": current.get("relevance_reason", ""), "publisher": current.get("publisher"),
        }, existing_urls=self._existing_url_index(), source_origin=current.get("source_origin", "external"))
        refreshed["review_state"] = current.get("review_state", "proposed")
        refreshed["reviewer_notes"] = current.get("reviewer_notes", "")
        package["candidate_sources"][index] = refreshed
        package["last_checked_at"] = self._now()
        package["history"].append({"event": "candidate_refreshed", "at": package["last_checked_at"],
                                   "actor": "Human", "source_candidate_id": candidate_id})
        self._save(package)
        self._sync_reference(package)
        return deepcopy(package)

    def _existing_sources(self, package: dict[str, Any], gap: dict[str, Any],
                          campaign: dict[str, Any]) -> list[dict[str, Any]]:
        area = self._area(campaign, gap["area_id"])
        terms = area.get("terms", [])
        found = []
        seen = set()
        for record in CuratorInventory(self.repository_root).collect():
            if record.content_type not in {"article", "command"}:
                continue
            search_text = json.dumps(record.raw, ensure_ascii=False).casefold()
            if not any(term.casefold() in search_text for term in terms):
                continue
            for source in record.raw.get("sources") or []:
                if not isinstance(source, dict) or not source.get("url"):
                    continue
                try:
                    canonical = canonicalize_url(source["url"])
                except KnowledgeSourceResearchError:
                    continue
                if canonical in seen:
                    continue
                seen.add(canonical)
                try:
                    candidate = self._validate_candidate(
                        package,
                        {"title": source.get("title"), "url": canonical, "publisher": "", "summary": record.title},
                        existing_urls=self._existing_url_index(), source_origin="existing_gnojo",
                        existing_match={"content_type": record.content_type, "identifier": record.identifier,
                                        "state": record.state, "source_path": record.source_path},
                    )
                except KnowledgeSourceResearchError:
                    continue
                if candidate["topic_relevant"]:
                    found.append(candidate)
        return self._rank(found)

    def _external_candidates(self, package: dict[str, Any], gap: dict[str, Any],
                             campaign: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        target = self.policy.target(package["platform"], package.get("product_vendor"))
        if not target:
            raise KnowledgeSourceResearchError("No approved external research target is configured for this platform.")
        provider = self.search_providers.get(target["search_provider"])
        if provider is None:
            raise KnowledgeSourceResearchError("The configured external research provider is unavailable.")
        area = self._area(campaign, gap["area_id"])
        query = " ".join(filter(None, [target.get("vendor"), package["platform"], area["title"],
                                       gap.get("facet", "").replace("_", " "), "official documentation"]))
        try:
            results = provider.search(query, domains=target.get("domains", []), limit=8)
        except Exception as error:
            raise KnowledgeSourceResearchError("External documentation search is temporarily unavailable.") from error
        existing_urls = self._existing_url_index()
        candidates = []
        seen = set()
        for result in results:
            try:
                canonical = canonicalize_url(result.get("url"))
                if canonical in seen or not any(SourceAuthorityPolicy._domain_matches(
                        urlsplit(canonical).hostname or "", domain) for domain in target.get("domains", [])):
                    continue
                seen.add(canonical)
                candidate = self._validate_candidate(package, result, existing_urls=existing_urls,
                                                     source_origin="external")
            except KnowledgeSourceResearchError as error:
                candidates.append(self._rejected_candidate(package, result, str(error)))
                continue
            candidates.append(candidate)
        return query, self._rank(self._deduplicate(candidates))

    def _validate_candidate(self, package: dict[str, Any], source: dict[str, Any], *,
                            existing_urls: dict[str, dict[str, Any]], source_origin: str,
                            existing_match: dict[str, Any] | None = None) -> dict[str, Any]:
        canonical = canonicalize_url(source.get("url"))
        inspected = self.http_validator.inspect(canonical)
        final_url = canonicalize_url(inspected["final_url"])
        authority = self.policy.classify(final_url)
        if authority["authority_tier"] is None:
            raise KnowledgeSourceResearchError("Publisher is not classified by the authoritative-source policy.")
        page_title = inspected.get("page_title") or " ".join(str(source.get("title") or "").split())
        topic_relevant, reason = self._relevance(package, source, page_title, inspected.get("content_preview", ""))
        if not topic_relevant:
            raise KnowledgeSourceResearchError("Resolved page does not contain enough evidence for the requested topic.")
        match = existing_match or existing_urls.get(final_url) or existing_urls.get(canonical)
        return {
            "source_candidate_id": self._stable_id("KSC", package["package_id"], final_url),
            "canonical_url": final_url, "page_title": page_title,
            "publisher": source.get("publisher") or authority["publisher"],
            "domain": urlsplit(final_url).hostname, "source_type": authority["authority_label"],
            "retrieved_at": self._now(), "http_status": inspected["http_status"],
            "final_resolved_url": final_url, "redirect_chain": inspected.get("redirect_chain", []),
            "freshness": {"last_modified": inspected.get("last_modified"),
                          "search_last_updated": source.get("last_updated"), "etag": inspected.get("etag")},
            "applicable_platform": package.get("platform"), "applicable_product": package.get("product_vendor"),
            **authority, "relevance_confidence": "high" if page_title else "medium",
            "relevance_reason": reason, "supported_coverage_facet": package.get("coverage_facet"),
            "supported_gap_id": package.get("gap_id"), "supported_work_item_id": package.get("work_item_id"),
            "topic_relevant": True, "duplicate_status": "existing_gnojo_source" if match else "unique",
            "existing_gnojo_source_match": match, "source_origin": source_origin,
            "review_state": "proposed", "reviewer_notes": "",
            "provenance": {"content_digest": inspected.get("content_digest"),
                           "checked_at": self._now(), "content_type": inspected.get("content_type")},
        }

    def _rejected_candidate(self, package: dict[str, Any], source: dict[str, Any], reason: str) -> dict[str, Any]:
        raw_url = str(source.get("url") or "")
        try:
            canonical = canonicalize_url(raw_url)
        except KnowledgeSourceResearchError:
            canonical = raw_url
        return {
            "source_candidate_id": self._stable_id("KSC", package["package_id"], canonical),
            "canonical_url": canonical, "page_title": str(source.get("title") or "Unknown source"),
            "publisher": source.get("publisher"), "domain": urlsplit(canonical).hostname,
            "source_type": "Unverified", "retrieved_at": self._now(), "http_status": None,
            "final_resolved_url": None, "authority_tier": None, "authority_label": "Unverified",
            "relevance_confidence": "low", "relevance_reason": reason,
            "supported_coverage_facet": package.get("coverage_facet"),
            "supported_gap_id": package.get("gap_id"), "supported_work_item_id": package.get("work_item_id"),
            "topic_relevant": False, "duplicate_status": "unknown",
            "existing_gnojo_source_match": None, "source_origin": "external",
            "review_state": "rejected", "reviewer_notes": reason,
            "provenance": {"checked_at": self._now(), "limitation": reason},
        }

    def _relevance(self, package: dict[str, Any], source: dict[str, Any], title: str,
                   content: str) -> tuple[bool, str]:
        campaign = self.planner.get(package["campaign_id"])
        area = self._area(campaign, package["target_coverage_area"])
        terms = [term.casefold() for term in area.get("terms", [])]
        haystack = " ".join((title, str(source.get("summary") or ""), content[:65536])).casefold()
        matched = sorted({term for term in terms if term in haystack})
        if not matched:
            return False, "No configured coverage terms were found in the resolved source."
        return True, f"Resolved content matched the configured {area['title']} terms: {', '.join(matched[:5])}."

    def _existing_url_index(self) -> dict[str, dict[str, Any]]:
        index = {}
        for record in CuratorInventory(self.repository_root).collect():
            for source in record.raw.get("sources") or []:
                if not isinstance(source, dict) or not source.get("url"):
                    continue
                try:
                    canonical = canonicalize_url(source["url"])
                except KnowledgeSourceResearchError:
                    continue
                index.setdefault(canonical, {"content_type": record.content_type,
                                             "identifier": record.identifier, "state": record.state,
                                             "source_path": record.source_path})
        return index

    def _context(self, campaign_id: str, gap_id: str, work_item_id: str):
        try:
            campaign = self.planner.get(campaign_id)
        except KnowledgeCoveragePlannerError as error:
            raise KnowledgeSourceResearchError(str(error)) from error
        gap = next((item for item in campaign.get("gaps", []) if item["gap_id"] == gap_id), None)
        work = next((item for item in campaign.get("work_items", [])
                     if item["work_item_id"] == work_item_id and item["gap_id"] == gap_id), None)
        if gap is None or work is None:
            raise KnowledgeSourceResearchError("Research must reference a current campaign gap and work item.")
        workflow_types = {"workflow", "workflow_branch", "verification_step",
                          "escalation_path", "safety_review"}
        source_gap = gap.get("gap_type") == "missing_source" and work.get("work_type") == "source_research"
        workflow_gap = work.get("work_type") in workflow_types
        if not (source_gap or workflow_gap):
            raise KnowledgeSourceResearchError(
                "Phase 2 research requires an authoritative-source gap or a workflow-oriented work item."
            )
        return campaign, gap, work

    def _area(self, campaign: dict[str, Any], area_id: str) -> dict[str, Any]:
        domain = next((item for item in self.planner.domains() if item["id"] == campaign["domain"]), None)
        area = next((item for item in domain.get("areas", []) if item["id"] == area_id), None) if domain else None
        if not area:
            raise KnowledgeSourceResearchError("Campaign coverage area is no longer defined.")
        return area

    def _attach_reference(self, campaign: dict[str, Any], package: dict[str, Any]) -> None:
        refs = campaign.setdefault("research_packages", [])
        if not any(item["package_id"] == package["package_id"] for item in refs):
            refs.append(self._reference(package))
            campaign["history"].append({"event": "research_package_created", "at": self._now(),
                                        "actor": "Human", "package_id": package["package_id"]})
            self.planner._save(campaign)

    def _sync_reference(self, package: dict[str, Any]) -> None:
        campaign = self.planner.get(package["campaign_id"])
        refs = campaign.setdefault("research_packages", [])
        replacement = self._reference(package)
        for index, item in enumerate(refs):
            if item["package_id"] == package["package_id"]:
                refs[index] = replacement
                break
        else:
            refs.append(replacement)
        self.planner._save(campaign)

    @staticmethod
    def _reference(package: dict[str, Any]) -> dict[str, Any]:
        return {"package_id": package["package_id"], "gap_id": package["gap_id"],
                "work_item_id": package["work_item_id"], "area_id": package["target_coverage_area"],
                "status": package["status"], "last_checked_at": package.get("last_checked_at")}

    def _path(self, package_id: str) -> Path:
        if not re.fullmatch(r"KRP-[A-F0-9]{12}", str(package_id or "")):
            raise KnowledgeSourceResearchError("Invalid research package ID.")
        return self.package_root / f"{package_id}.json"

    def _save(self, package: dict[str, Any]) -> None:
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(package["package_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(package, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeSourceResearchError(f"Unable to read research package: {error}") from error

    @staticmethod
    def _merge_candidates(existing: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return KnowledgeSourceResearchService._rank(
            KnowledgeSourceResearchService._deduplicate(existing + discovered))

    @staticmethod
    def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = {}
        for candidate in candidates:
            key = candidate.get("canonical_url") or candidate["source_candidate_id"]
            current = result.get(key)
            if current is None or (current.get("topic_relevant") is False and candidate.get("topic_relevant") is True):
                result[key] = candidate
        return list(result.values())

    @staticmethod
    def _rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(candidates, key=lambda item: (
            item.get("topic_relevant") is not True,
            item.get("authority_tier") if item.get("authority_tier") is not None else 99,
            item.get("duplicate_status") != "existing_gnojo_source",
            item.get("page_title") or "",
        ))

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
