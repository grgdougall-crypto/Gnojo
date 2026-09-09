import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchError,
    KnowledgeSourceResearchService,
    SourceAuthorityPolicy,
    SourceHTTPValidator,
    canonicalize_url,
)


class FakeValidator:
    def __init__(self, failures=None, redirects=None):
        self.calls = []
        self.failures = failures or {}
        self.redirects = redirects or {}

    def inspect(self, url):
        self.calls.append(url)
        if url in self.failures:
            raise KnowledgeSourceResearchError(self.failures[url])
        final = self.redirects.get(url, url)
        topic = "VPN official troubleshooting connectivity documentation"
        return {"http_status": 200, "final_url": final, "redirect_chain": [final] if final != url else [],
                "page_title": "Troubleshoot VPN Connections in Windows", "content_type": "text/html",
                "last_modified": "Fri, 01 Aug 2026 00:00:00 GMT", "etag": "stable",
                "content_digest": "abc123", "content_preview": topic}


class FakeSearch:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []

    def search(self, query, *, domains, limit):
        self.calls.append((query, tuple(domains), limit))
        if self.error:
            raise self.error
        return deepcopy(self.results)


class FakeHTTPResponse:
    def __init__(self, status, *, location="", body=b"<title>VPN help</title>"):
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}
        if location:
            self.headers["Location"] = location
        self.body = body

    def iter_content(self, chunk_size=8192):
        yield self.body

    def close(self):
        pass


class RedirectingSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url.endswith("/docs"):
            return FakeHTTPResponse(301, location="/docs/")
        return FakeHTTPResponse(200)


class KnowledgeSourceResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
                          "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/archive",
                          "knowledge_base/commands", "knowledge_base/scripts"):
            (self.root / directory).mkdir(parents=True)
        taxonomy = {"schema_version": "1.0", "domains": [{"id": "windows-connectivity",
            "title": "Windows Connectivity", "category": "Networking", "platforms": ["Windows"],
            "areas": [{"id": "vpn", "title": "VPN", "terms": ["vpn", "virtual private network"]}]}]}
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps(taxonomy), encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": "1.0", "tiers": [
            {"tier": 1, "label": "First-party", "publishers": [
                {"name": "Microsoft", "domains": ["learn.microsoft.com", "support.microsoft.com"]}]},
            {"tier": 2, "label": "Standards", "publishers": [
                {"name": "IETF", "domains": ["rfc-editor.org"]}]}],
            "research_targets": [{"platform": "Windows", "vendor": "Microsoft",
                "search_provider": "microsoft_learn", "domains": ["learn.microsoft.com", "support.microsoft.com"]}]
        }), encoding="utf-8")
        self.campaign_root = self.root / "campaigns"
        planner = KnowledgeCoveragePlannerService(self.root, self.campaign_root, self.taxonomy)
        campaign = planner.create(title="Windows Connectivity", domain_id="windows-connectivity",
                                  objective="Fill trusted evidence gaps")
        analyzed = planner.analyze(campaign["campaign_id"])
        self.gap = next(item for item in analyzed["gaps"] if item["gap_type"] == "missing_source")
        self.work = next(item for item in analyzed["work_items"] if item["gap_id"] == self.gap["gap_id"])
        self.search = FakeSearch([{"title": "VPN help", "url": "https://learn.microsoft.com/windows/vpn-help?utm_source=x",
                                  "summary": "VPN connectivity", "publisher": "Microsoft"}])
        self.validator = FakeValidator()
        self.service = KnowledgeSourceResearchService(
            self.root, self.campaign_root, self.policy, {"microsoft_learn": self.search},
            self.validator, self.taxonomy)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self):
        return self.service.create(self.work["campaign_id"], self.gap["gap_id"], self.work["work_item_id"])

    def write_article(self, url="https://support.microsoft.com/windows/vpn-existing"):
        article = {"schema_version": "1.0", "id": "vpn-guide", "canonical_id": "vpn-guide",
                   "title": "VPN Guide", "category": "Networking", "tags": ["vpn"],
                   "overview": "VPN connectivity", "sources": [{"title": "VPN source", "url": url}],
                   "review": {"status": "approved"}}
        path = self.root / "knowledge_base/published/vpn-guide.json"
        path.write_text(json.dumps(article), encoding="utf-8")
        return path

    def capability_article_service(self):
        taxonomy = {
            "schema_version": "1.0",
            "domains": [{
                "id": "desktop-support", "title": "Desktop Support",
                "category": "Desktop Support", "platforms": ["Windows"],
                "capability_catalog": "capabilities.json", "areas": [],
            }],
        }
        catalog = {
            "schema_version": "1.0", "catalog_id": "test-capabilities",
            "domain_id": "desktop-support", "title": "Test capabilities",
            "capabilities": [{
                "id": "vpn-article", "title": "VPN Article", "level": "core",
                "platform": "Windows", "category": "Networking",
                "terms": ["vpn", "connectivity"],
                "expected_artifacts": ["article"], "likely_relationships": [],
                "artifact_matches": {"workflow": [], "article": [], "command": []},
            }],
        }
        self.taxonomy.write_text(json.dumps(taxonomy), encoding="utf-8")
        (self.root / "capabilities.json").write_text(json.dumps(catalog), encoding="utf-8")
        planner = KnowledgeCoveragePlannerService(
            self.root, self.campaign_root, self.taxonomy
        )
        identity = "capability:desktop-support:windows:vpn-article:missing_article"
        candidate = {
            "gap_identity": identity, "gap_type": "missing_article",
            "domain_id": "desktop-support", "area_id": "vpn-article",
            "area_title": "VPN Article", "capability_id": "vpn-article",
            "capability_level": "core", "capability_category": "Networking",
            "platform": "Windows", "expected_artifacts": ["article"],
            "likely_relationships": [], "evidence": ["Article coverage is absent."],
        }
        campaign = planner.create(
            title="VPN Article Coverage", domain_id="desktop-support",
            objective="Prepare a governed article draft.",
            actor="Autonomous Growth Stage 1",
            metadata={
                "initiated_by": "autonomous_growth_stage1",
                "gap_identity": identity,
                "selected_gap": candidate,
            },
        )
        campaign = planner.analyze(campaign["campaign_id"])
        gap = campaign["gaps"][0]
        work = campaign["work_items"][0]
        service = KnowledgeSourceResearchService(
            self.root, self.campaign_root, self.policy,
            {"microsoft_learn": self.search}, self.validator, self.taxonomy,
        )
        return service, campaign, gap, work

    def test_request_has_stable_identity_associations_defaults_and_history(self):
        first = self.create(); second = self.create()
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertEqual((first["campaign_id"], first["gap_id"], first["work_item_id"]),
                         (self.work["campaign_id"], self.gap["gap_id"], self.work["work_item_id"]))
        self.assertEqual(first["status"], "pending"); self.assertEqual(first["history"][0]["event"], "created")

    def test_package_and_campaign_reference_persist(self):
        package = self.create()
        self.assertEqual(self.service.get(package["package_id"])["package_id"], package["package_id"])
        campaign = self.service.planner.get(package["campaign_id"])
        self.assertEqual(campaign["research_packages"][0]["package_id"], package["package_id"])

    def test_existing_source_is_checked_first_and_reused_without_search(self):
        self.write_article(); package = self.service.run(self.create()["package_id"])
        self.assertTrue(package["reuse_recommendation"]["recommended"])
        self.assertEqual(self.search.calls, []); self.assertEqual(package["research_query"], None)
        self.assertEqual(package["candidate_sources"][0]["duplicate_status"], "existing_gnojo_source")

    def test_force_external_uses_deterministic_targeted_query(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        self.assertIn("Microsoft Windows VPN", package["research_query"])
        self.assertEqual(self.search.calls[0][1], ("learn.microsoft.com", "support.microsoft.com"))

    def test_capability_missing_article_enters_catalog_governed_research(self):
        service, campaign, gap, work = self.capability_article_service()

        package = service.create(campaign["campaign_id"], gap["gap_id"], work["work_item_id"])
        researched = service.run(package["package_id"], force_external=True)

        context = researched["capability_article_context"]
        self.assertEqual(context["gap_identity"], gap["gap_identity"])
        self.assertEqual(context["capability_id"], "vpn-article")
        self.assertEqual(context["expected_artifact"], "article")
        self.assertEqual(context["terms"], ["vpn", "connectivity"])
        self.assertIn("VPN Article", researched["research_query"])
        self.assertEqual(researched["status"], "ready_for_review")

    def test_capability_missing_article_identity_mismatch_fails_closed(self):
        service, campaign, gap, work = self.capability_article_service()
        persisted = service.planner.get(campaign["campaign_id"])
        persisted["work_items"][0]["gap_identity"] = (
            "capability:desktop-support:windows:other:missing_article"
        )
        service.planner._save(persisted)

        with self.assertRaisesRegex(
            KnowledgeSourceResearchError, "identity is missing, stale, or ambiguous"
        ):
            service.create(campaign["campaign_id"], gap["gap_id"], work["work_item_id"])

    def test_capability_missing_article_package_fails_if_catalog_terms_change(self):
        service, campaign, gap, work = self.capability_article_service()
        package = service.create(campaign["campaign_id"], gap["gap_id"], work["work_item_id"])
        catalog_path = self.root / "capabilities.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["capabilities"][0]["terms"] = ["changed governed term"]
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        with self.assertRaisesRegex(
            KnowledgeSourceResearchError, "context is missing or stale"
        ):
            service.run(package["package_id"], force_external=True)

    def test_canonicalization_removes_tracking_fragment_and_duplicate_results(self):
        self.search.results *= 2
        package = self.service.run(self.create()["package_id"], force_external=True)
        self.assertEqual(len(package["candidate_sources"]), 1)
        self.assertEqual(package["candidate_sources"][0]["canonical_url"],
                         "https://learn.microsoft.com/windows/vpn-help")
        self.assertEqual(canonicalize_url("https://EXAMPLE.com/a/?utm_source=x#z"), "https://example.com/a")

    def test_authority_policy_classifies_subdomains_and_prefers_first_party(self):
        policy = SourceAuthorityPolicy(self.policy)
        self.assertEqual(policy.classify("https://sub.support.microsoft.com/x")["authority_tier"], 1)
        self.assertEqual(policy.classify("https://rfc-editor.org/rfc/1")["authority_tier"], 2)

    def test_resolution_redirect_and_freshness_metadata_are_recorded(self):
        old = "https://learn.microsoft.com/windows/vpn-help"
        self.validator.redirects[old] = "https://support.microsoft.com/windows/vpn-current"
        source = self.service.run(self.create()["package_id"], force_external=True)["candidate_sources"][0]
        self.assertEqual(source["http_status"], 200); self.assertEqual(source["final_resolved_url"], self.validator.redirects[old])
        self.assertTrue(source["redirect_chain"]); self.assertEqual(source["freshness"]["etag"], "stable")

    def test_duplicate_final_url_preserves_reuse_provenance_as_one_candidate(self):
        first = {
            "source_candidate_id": "KSC-SAME", "canonical_url": "https://support.example/final",
            "topic_relevant": True, "existing_gnojo_source_match": {
                "content_type": "article", "identifier": "article-one",
                "source_path": "knowledge_base/published/article-one.json",
            },
        }
        second = deepcopy(first)
        second["existing_gnojo_source_match"] = {
            "content_type": "article", "identifier": "article-two",
            "source_path": "knowledge_base/published/article-two.json",
        }

        result = KnowledgeSourceResearchService._deduplicate([first, second])

        self.assertEqual(len(result), 1)
        self.assertEqual(
            {item["identifier"] for item in result[0]["existing_gnojo_source_matches"]},
            {"article-one", "article-two"},
        )

    def test_http_validation_preserves_server_significant_trailing_slash(self):
        session = RedirectingSession()
        validator = SourceHTTPValidator(
            session=session,
            host_resolver=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        )
        result = validator.inspect("https://learn.microsoft.com/docs")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["final_url"], "https://learn.microsoft.com/docs/")
        self.assertEqual(session.urls, ["https://learn.microsoft.com/docs", "https://learn.microsoft.com/docs/"])

    def test_dead_and_irrelevant_candidates_are_rejected_not_selected(self):
        url = "https://learn.microsoft.com/windows/vpn-help"
        self.validator.failures[url] = "Source returned HTTP 404."
        package = self.service.run(self.create()["package_id"], force_external=True)
        source = package["candidate_sources"][0]
        self.assertEqual(source["review_state"], "rejected"); self.assertFalse(source["topic_relevant"])
        self.assertIn(source["source_candidate_id"], package["rejected_sources"])

    def test_candidate_selection_rejection_and_approval_are_human_gated(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        candidate = package["candidate_sources"][0]
        with self.assertRaises(KnowledgeSourceResearchError): self.service.review(package["package_id"], "approved")
        package = self.service.set_candidate_state(package["package_id"], candidate["source_candidate_id"], "selected")
        self.assertEqual(package["selected_sources"], [candidate["source_candidate_id"]])
        self.assertEqual(self.service.review(package["package_id"], "approved", "Reviewed")["status"], "approved")

    def test_duplicate_package_approval_is_write_and_history_free(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        candidate = package["candidate_sources"][0]
        self.service.set_candidate_state(
            package["package_id"], candidate["source_candidate_id"], "selected"
        )
        approved = self.service.review(package["package_id"], "approved", "Reviewed")
        path = self.service._path(package["package_id"])
        before = path.read_bytes()

        duplicate = self.service.review(package["package_id"], "approved", "Reviewed")

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(duplicate["history"]), len(approved["history"]))

    def test_rejection_state_and_notes_persist(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        candidate = package["candidate_sources"][0]
        saved = self.service.set_candidate_state(package["package_id"], candidate["source_candidate_id"], "rejected", "Too broad")
        self.assertEqual(saved["candidate_sources"][0]["reviewer_notes"], "Too broad")
        self.assertEqual(self.service.get(package["package_id"])["rejected_sources"], [candidate["source_candidate_id"]])

    def test_idempotent_rerun_does_not_fetch_or_duplicate_history(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        call_count = len(self.validator.calls); history_count = len(package["history"])
        rerun = self.service.run(package["package_id"])
        self.assertEqual(len(self.validator.calls), call_count); self.assertEqual(len(rerun["history"]), history_count)

    def test_candidate_refresh_revalidates_and_records_history(self):
        package = self.service.run(self.create()["package_id"], force_external=True)
        candidate = package["candidate_sources"][0]; before = len(self.validator.calls)
        refreshed = self.service.refresh_candidate(package["package_id"], candidate["source_candidate_id"])
        self.assertEqual(len(self.validator.calls), before + 1)
        self.assertEqual(refreshed["history"][-1]["event"], "candidate_refreshed")

    def test_network_failure_is_graceful_and_traceable(self):
        self.search.error = RuntimeError("offline")
        package = self.create()
        with self.assertRaisesRegex(KnowledgeSourceResearchError, "temporarily unavailable"):
            self.service.run(package["package_id"], force_external=True)
        failed = self.service.get(package["package_id"])
        self.assertEqual(failed["status"], "pending"); self.assertEqual(failed["history"][-1]["event"], "research_failed")

    def test_research_does_not_mutate_production_or_governance_state(self):
        protected = []
        for name in ("app/decision_trees/sentinel.json", "knowledge_base/published/sentinel.json",
                     "curation_memory/memory.json", "curation_memory/reasoning_calibration.json",
                     "curation_runs/latest.json"):
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8"); protected.append(path)
        before = {path: path.read_bytes() for path in protected}
        self.service.run(self.create()["package_id"], force_external=True)
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

    def test_invalid_context_and_non_source_work_are_rejected(self):
        with self.assertRaises(KnowledgeSourceResearchError):
            self.service.create(self.work["campaign_id"], "KCG-INVALID", self.work["work_item_id"])

    def test_ui_prepare_run_review_and_campaign_listing_routes(self):
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeCoveragePlannerService", return_value=self.service.planner), \
             patch("app.app.KnowledgeSourceResearchService", return_value=self.service):
            with flask_app.test_client() as client:
                detail = client.get(f"/curator/growth/coverage-campaigns/{self.work['campaign_id']}")
                self.assertEqual(detail.status_code, 200); self.assertIn(b"Prepare Research Package", detail.data)
                created = client.post(f"/curator/growth/coverage-campaigns/{self.work['campaign_id']}/research",
                    data={"gap_id": self.gap["gap_id"], "work_item_id": self.work["work_item_id"]})
                self.assertEqual(created.status_code, 302)
                package = self.service.list_for_campaign(self.work["campaign_id"])[0]
                page = client.get(f"/curator/growth/source-research/{package['package_id']}")
                self.assertIn(b"Run Research", page.data); self.assertIn(b"Human authority", page.data)
                run = client.post(f"/curator/growth/source-research/{package['package_id']}/run",
                                  data={"force_external": "true"})
                self.assertEqual(run.status_code, 302)

    def test_production_service_contains_no_llm_routing_or_content_builders(self):
        source = Path("app/services/knowledge_source_research_service.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("openai", source); self.assertNotIn("gemini", source)
        self.assertNotIn("articlebuilder", source); self.assertNotIn("workflowbuilder", source)


if __name__ == "__main__":
    unittest.main()
