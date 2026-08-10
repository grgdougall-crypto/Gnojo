import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)


class KnowledgeCoveragePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "app" / "decision_trees").mkdir(parents=True)
        (self.root / "app" / "workflow_drafts").mkdir(parents=True)
        (self.root / "app" / "workflow_publications").mkdir(parents=True)
        for name in ("drafts", "published", "archive", "commands", "scripts"):
            (self.root / "knowledge_base" / name).mkdir(parents=True)
        taxonomy = {
            "schema_version": "1.0", "domains": [{
                "id": "windows-connectivity", "title": "Windows Connectivity",
                "category": "Networking", "platforms": ["Windows"],
                "areas": [
                    {"id": "internet", "title": "Internet Access", "terms": ["internet"]},
                    {"id": "dns", "title": "DNS", "terms": ["dns", "nslookup"]},
                    {"id": "vpn", "title": "VPN", "terms": ["vpn"]},
                ],
            }],
        }
        self.taxonomy_path = self.root / "taxonomy.json"
        self.taxonomy_path.write_text(json.dumps(taxonomy), encoding="utf-8")
        self.campaign_root = self.root / "campaigns"
        self.service = KnowledgeCoveragePlannerService(
            self.root, self.campaign_root, self.taxonomy_path)

    def tearDown(self):
        self.temporary.cleanup()

    def write_workflow(self, name="internet", **overrides):
        workflow = {
            "workflow_id": name, "name": "Internet and DNS", "category": "Networking",
            "platform": "Windows", "start_node": "inspect", "nodes": {
                "inspect": {"type": "instruction", "title": "Inspect DNS",
                            "instruction": "Run nslookup for internet name resolution.",
                            "knowledge_article": "dns-guide", "next": "verify"},
                "verify": {"type": "question", "question": "Did DNS return an address?",
                           "answers": [{"label": "Yes", "next": "done"},
                                       {"label": "No", "next": "escalate"}]},
                "done": {"type": "resolution", "title": "Internet restored"},
                "escalate": {"type": "transition", "title": "Escalate DNS",
                             "next_workflow": "advanced_connectivity"},
            },
        }
        workflow.update(overrides)
        path = self.root / "app" / "decision_trees" / f"{name}.json"
        path.write_text(json.dumps(workflow), encoding="utf-8")
        return path

    def write_article(self, identifier="dns-guide", **overrides):
        article = {
            "schema_version": "1.0", "id": identifier, "canonical_id": identifier,
            "title": "DNS Internet Guide", "category": "Networking", "tags": ["dns", "internet"],
            "overview": "DNS internet name resolution.", "sources": [
                {"title": "Official DNS guide", "url": "https://example.test/dns"}
            ], "review": {"status": "approved"},
        }
        article.update(overrides)
        path = self.root / "knowledge_base" / "published" / f"{identifier}.json"
        path.write_text(json.dumps(article), encoding="utf-8")
        return path

    def create(self):
        return self.service.create(
            title="Windows Connectivity Pilot", domain_id="windows-connectivity",
            objective="Plan deterministic coverage improvements.")

    def test_campaign_creation_has_stable_identity_defaults_and_history(self):
        campaign = self.create()
        self.assertRegex(campaign["campaign_id"], r"^KCP-[A-F0-9]{12}$")
        self.assertEqual(campaign["status"], "draft")
        self.assertEqual(campaign["history"][0]["event"], "created")
        self.assertTrue((self.campaign_root / f"{campaign['campaign_id']}.json").exists())

    def test_creation_rejects_unknown_scope_and_missing_required_fields(self):
        with self.assertRaises(KnowledgeCoveragePlannerError):
            self.service.create(title="", domain_id="windows-connectivity", objective="Plan")
        with self.assertRaises(KnowledgeCoveragePlannerError):
            self.service.create(title="Plan", domain_id="linux", objective="Plan")

    def test_analysis_discovers_existing_workflow_article_source_and_relationship(self):
        self.write_workflow()
        self.write_article()
        analyzed = self.service.analyze(self.create()["campaign_id"])
        dns = next(item for item in analyzed["coverage_snapshot"]["areas"] if item["area_id"] == "dns")
        self.assertTrue(dns["facets"]["workflow"])
        self.assertTrue(dns["facets"]["article"])
        self.assertTrue(dns["facets"]["provenance_source"])
        self.assertTrue(dns["facets"]["verification"])
        self.assertTrue(dns["facets"]["escalation"])
        self.assertTrue(dns["facets"]["relationships_reuse"])

    def test_partial_and_missing_coverage_produce_structured_gaps(self):
        self.write_workflow()
        analyzed = self.service.analyze(self.create()["campaign_id"])
        gap_types = {item["gap_type"] for item in analyzed["gaps"]}
        self.assertIn("missing_article", gap_types)
        self.assertIn("missing_workflow", gap_types)
        self.assertTrue(all(item.get("gap_id") and item.get("evidence") for item in analyzed["gaps"]))

    def test_gaps_generate_proposed_work_items_with_traceable_stable_ids(self):
        analyzed = self.service.analyze(self.create()["campaign_id"])
        self.assertTrue(analyzed["work_items"])
        for item in analyzed["work_items"]:
            self.assertEqual(item["status"], "proposed")
            self.assertTrue(item["gap_id"].startswith("KCG-"))
            self.assertTrue(item["work_item_id"].startswith("KCW-"))
            self.assertEqual(item["campaign_id"], analyzed["campaign_id"])

    def test_reanalysis_is_idempotent_and_does_not_duplicate_history_or_work(self):
        self.write_workflow()
        campaign_id = self.create()["campaign_id"]
        first = self.service.analyze(campaign_id)
        second = self.service.analyze(campaign_id)
        self.assertEqual(first["coverage_snapshot"]["fingerprint"], second["coverage_snapshot"]["fingerprint"])
        self.assertEqual(first["gaps"], second["gaps"])
        self.assertEqual(first["work_items"], second["work_items"])
        self.assertEqual(len(first["history"]), len(second["history"]))

    def test_changed_inventory_adds_one_analysis_history_event(self):
        campaign_id = self.create()["campaign_id"]
        first = self.service.analyze(campaign_id)
        self.write_workflow()
        second = self.service.analyze(campaign_id)
        self.assertEqual(len(second["history"]), len(first["history"]) + 1)

    def test_shared_article_is_reported_as_reuse_opportunity(self):
        self.write_article()
        self.write_workflow("internet-one")
        self.write_workflow("internet-two")
        analyzed = self.service.analyze(self.create()["campaign_id"])
        reuse = next(item for item in analyzed["reuse_opportunities"] if item["article_id"] == "dns-guide")
        self.assertEqual(reuse["workflow_ids"], ["internet-one", "internet-two"])

    def test_campaign_persists_and_lists_without_touching_content(self):
        workflow_path = self.write_workflow()
        article_path = self.write_article()
        before = (workflow_path.read_bytes(), article_path.read_bytes())
        campaign = self.service.analyze(self.create()["campaign_id"])
        reloaded = self.service.get(campaign["campaign_id"])
        self.assertEqual(reloaded["campaign_id"], campaign["campaign_id"])
        self.assertEqual(self.service.list_campaigns()[0]["campaign_id"], campaign["campaign_id"])
        self.assertEqual(before, (workflow_path.read_bytes(), article_path.read_bytes()))

    def test_analysis_does_not_create_workflows_articles_or_curator_memory(self):
        campaign_id = self.create()["campaign_id"]
        protected_roots = (
            self.root / "app" / "decision_trees",
            self.root / "app" / "workflow_drafts",
            self.root / "app" / "workflow_publications",
            self.root / "knowledge_base" / "drafts",
            self.root / "knowledge_base" / "published",
            self.root / "knowledge_base" / "archive",
        )
        before_files = {path: path.read_bytes() for root in protected_roots for path in root.glob("*.json")}
        self.service.analyze(campaign_id)
        after_files = {path: path.read_bytes() for root in protected_roots for path in root.glob("*.json")}
        self.assertEqual(before_files, after_files)
        self.assertFalse((self.root / "curation_memory").exists())

    def test_analysis_does_not_mutate_curator_calibration_health_or_debt_files(self):
        protected = {}
        for name in ("curation_memory/memory.json", "curation_memory/reasoning_calibration.json",
                     "curation_runs/latest.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8")
            protected[path] = path.read_bytes()
        self.service.analyze(self.create()["campaign_id"])
        self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_taxonomy_is_separate_extensible_pilot_data(self):
        domains = self.service.domains()
        self.assertEqual(domains[0]["id"], "windows-connectivity")
        self.assertEqual({item["id"] for item in domains[0]["areas"]}, {"internet", "dns", "vpn"})

    def test_analysis_has_no_ai_or_internet_dependencies(self):
        source = Path("app/services/knowledge_coverage_planner_service.py").read_text(encoding="utf-8")
        self.assertNotIn("requests", source)
        self.assertNotIn("openai", source.casefold())
        self.assertNotIn("gemini", source.casefold())

    def test_campaign_ui_lists_creates_opens_and_analyzes_campaign(self):
        facade = KnowledgeCoveragePlannerService(self.root, self.campaign_root, self.taxonomy_path)
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeCoveragePlannerService", return_value=facade):
            with flask_app.test_client() as client:
                listing = client.get("/curator/growth/coverage-campaigns")
                self.assertEqual(listing.status_code, 200)
                self.assertIn(b"Coverage Campaigns", listing.data)
                created = client.post("/curator/growth/coverage-campaigns", data={
                    "title": "Connectivity", "domain": "windows-connectivity", "objective": "Plan gaps"
                })
                self.assertEqual(created.status_code, 302)
                campaign_id = facade.list_campaigns()[0]["campaign_id"]
                detail = client.get(f"/curator/growth/coverage-campaigns/{campaign_id}")
                self.assertIn(b"Analyze Coverage", detail.data)
                analyzed = client.post(f"/curator/growth/coverage-campaigns/{campaign_id}/analyze")
                self.assertEqual(analyzed.status_code, 302)
                self.assertEqual(facade.get(campaign_id)["status"], "analyzed")

    def test_growth_page_exposes_planner_once(self):
        source = Path("app/templates/curator_growth.html").read_text(encoding="utf-8")
        self.assertEqual(source.count("Open Coverage Planner"), 1)

    def test_invalid_campaign_identifier_is_rejected(self):
        with self.assertRaises(KnowledgeCoveragePlannerError):
            self.service.get("../memory")


if __name__ == "__main__":
    unittest.main()
