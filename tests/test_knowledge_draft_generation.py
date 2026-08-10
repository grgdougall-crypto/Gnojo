import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_draft_generation_service import (
    KnowledgeDraftGenerationError,
    KnowledgeDraftGenerationService,
)


class KnowledgeDraftGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in (
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
            "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/archive",
            "knowledge_base/commands", "knowledge_base/scripts",
        ):
            (self.root / directory).mkdir(parents=True)
        taxonomy = {
            "schema_version": "1.0",
            "domains": [{
                "id": "windows-connectivity", "title": "Windows Connectivity",
                "category": "Networking", "platforms": ["Windows"],
                "areas": [{
                    "id": "vpn", "title": "VPN", "terms": ["vpn", "connectivity"],
                }],
            }],
        }
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps(taxonomy), encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({
            "schema_version": "1.0",
            "tiers": [{"tier": 1, "label": "First-party", "publishers": [{
                "name": "Microsoft", "domains": ["learn.microsoft.com"],
            }]}],
            "research_targets": [{
                "platform": "Windows", "vendor": "Microsoft",
                "search_provider": "microsoft_learn", "domains": ["learn.microsoft.com"],
            }],
        }), encoding="utf-8")
        self.campaign_root = self.root / "campaigns"
        planner = KnowledgeCoveragePlannerService(self.root, self.campaign_root, self.taxonomy)
        campaign = planner.create(
            title="Windows Connectivity Pilot", domain_id="windows-connectivity",
            objective="Create trusted Windows connectivity guidance.",
        )
        campaign = planner.analyze(campaign["campaign_id"])
        self.gap = next(item for item in campaign["gaps"] if item["gap_type"] == "missing_article")
        self.work = next(item for item in campaign["work_items"] if item["gap_id"] == self.gap["gap_id"])
        self.repository = KnowledgeRepository(self.root / "knowledge_base")
        self.service = KnowledgeDraftGenerationService(
            self.root, self.campaign_root, self.taxonomy, self.policy, self.repository,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        return self.service.prepare(
            self.work["campaign_id"], self.gap["gap_id"], self.work["work_item_id"],
        )

    def approve_source_evidence(self):
        package_root = self.campaign_root / "research"
        package_root.mkdir(parents=True, exist_ok=True)
        package = {
            "schema_version": "1.0", "package_id": "KRP-AAAAAAAAAAAA",
            "campaign_id": self.work["campaign_id"], "gap_id": "KCG-SOURCE",
            "work_item_id": "KCW-SOURCE", "target_coverage_area": "vpn",
            "status": "approved", "created_at": "2026-08-10T00:00:00+00:00",
            "selected_sources": ["KSC-MICROSOFT"],
            "candidate_sources": [{
                "source_candidate_id": "KSC-MICROSOFT", "review_state": "selected",
                "topic_relevant": True,
                "canonical_url": "https://learn.microsoft.com/windows/security/operating-system-security/network-security/vpn/",
                "page_title": "VPN technical guide", "authority_tier": 1,
                "provenance": {"content_digest": "approved-digest"},
            }],
        }
        (package_root / "KRP-AAAAAAAAAAAA.json").write_text(
            json.dumps(package), encoding="utf-8",
        )
        return package

    def write_canonical_article(self):
        article = create_article_template()
        article.update({
            "id": "windows-vpn-troubleshooting-guide",
            "canonical_id": "windows-vpn-troubleshooting-guide",
            "title": "VPN Troubleshooting Guide", "category": "Networking",
            "estimated_time": "5 to 10 minutes", "overview": "Existing VPN guide.",
            "sources": [{"title": "Official VPN guide", "url": "https://learn.microsoft.com/vpn"}],
            "generation": {"provider": "Human", "model": "manual", "generated_at": "2026-08-10"},
            "review": {"status": "approved", "reviewed_by": "Reviewer",
                       "reviewed_at": "2026-08-10", "notes": []},
        })
        self.repository.save_published(article)
        return article

    def test_prepare_is_explicit_stable_idempotent_and_needs_evidence(self):
        self.assertEqual(self.service.list_for_campaign(self.work["campaign_id"]), [])
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertEqual(first["generation_status"], "needs_evidence")
        self.assertIsNone(first["draft_preview"])
        self.assertEqual(len(first["history"]), len(second["history"]))
        self.assertEqual(self.repository.get_drafts(), [])
        self.assertEqual(self.repository.get_published(), [])

    def test_approved_evidence_creates_valid_grounded_preview_with_provenance(self):
        research = self.approve_source_evidence()
        package = self.prepare()
        self.assertEqual(package["generation_status"], "ready_for_review")
        self.assertEqual(ArticleValidator.validate(package["draft_preview"]), [])
        self.assertEqual(package["research_package_ids"], [research["package_id"]])
        self.assertEqual(package["approved_sources_used"], package["draft_preview"]["sources"])
        self.assertEqual(package["source_provenance"][0]["content_digest"], "approved-digest")
        factory = package["draft_preview"]["knowledge_factory"]
        self.assertEqual(factory["campaign_id"], self.work["campaign_id"])
        self.assertEqual(factory["gap_id"], self.gap["gap_id"])
        self.assertEqual(factory["work_item_id"], self.work["work_item_id"])
        self.assertEqual(package["history"][-1]["actor"], "Deterministic Draft Composer")

    def test_canonical_identity_is_reused_instead_of_numbered_duplicate(self):
        existing = self.write_canonical_article()
        package = self.prepare()
        self.assertEqual(package["generation_status"], "superseded")
        self.assertEqual(package["reused_assets"][0]["identifier"], existing["id"])
        self.assertIsNone(package["draft_preview"])
        self.assertNotRegex(package["canonical_identity"], r"-\d+$")
        self.assertEqual(self.repository.get_drafts(), [])

    def test_handoff_is_human_gated_idempotent_and_never_publishes(self):
        self.approve_source_evidence()
        package = self.prepare()
        self.assertEqual(self.repository.get_drafts(), [])
        accepted = self.service.accept_into_content_studio(package["package_id"])
        accepted_again = self.service.accept_into_content_studio(package["package_id"])
        self.assertEqual(accepted["generation_status"], "accepted_into_content_studio")
        self.assertEqual(accepted_again["content_studio_article_id"], accepted["content_studio_article_id"])
        self.assertEqual(len(self.repository.get_drafts()), 1)
        self.assertEqual(self.repository.get_published(), [])
        saved = self.repository.get_drafts()[0]
        self.assertEqual(saved["knowledge_factory"]["generation_package_id"], package["package_id"])

    def test_invalid_or_blocked_work_is_not_eligible(self):
        campaign = self.service.planner.get(self.work["campaign_id"])
        campaign["work_items"][0]["dependencies"] = ["KCW-BLOCKER"]
        self.service.planner._save(campaign)
        with self.assertRaisesRegex(KnowledgeDraftGenerationError, "dependencies"):
            self.prepare()
        with self.assertRaises(KnowledgeDraftGenerationError):
            self.service.prepare(self.work["campaign_id"], "KCG-MISSING", self.work["work_item_id"])

    def test_rejected_or_unselected_research_is_not_used_as_evidence(self):
        self.approve_source_evidence()
        path = self.campaign_root / "research" / "KRP-AAAAAAAAAAAA.json"
        research = json.loads(path.read_text(encoding="utf-8"))
        research["status"] = "rejected"
        path.write_text(json.dumps(research), encoding="utf-8")
        package = self.prepare()
        self.assertEqual(package["generation_status"], "needs_evidence")
        self.assertEqual(package["approved_sources_used"], [])

    def test_generation_does_not_mutate_workflows_publications_or_curator_state(self):
        protected = []
        for name in (
            "app/decision_trees/sentinel.json", "app/workflow_publications/sentinel.json",
            "curation_memory/memory.json", "curation_memory/reasoning_calibration.json",
            "curation_runs/latest.json",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8")
            protected.append(path)
        before = {path: path.read_bytes() for path in protected}
        self.approve_source_evidence()
        self.prepare()
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

    def test_ui_exposes_prepare_review_and_explicit_content_studio_handoff(self):
        self.approve_source_evidence()
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeCoveragePlannerService", return_value=self.service.planner), \
             patch("app.app.KnowledgeDraftGenerationService", return_value=self.service):
            with flask_app.test_client() as client:
                campaign_page = client.get(
                    f"/curator/growth/coverage-campaigns/{self.work['campaign_id']}"
                )
                self.assertEqual(campaign_page.status_code, 200)
                self.assertIn(b"Prepare Draft", campaign_page.data)
                response = client.post(
                    f"/curator/growth/coverage-campaigns/{self.work['campaign_id']}/draft-generation",
                    data={"gap_id": self.gap["gap_id"], "work_item_id": self.work["work_item_id"]},
                )
                self.assertEqual(response.status_code, 302)
                package = self.service.list_for_campaign(self.work["campaign_id"])[0]
                detail = client.get(f"/curator/growth/draft-generation/{package['package_id']}")
                self.assertIn(b"Supervised Knowledge Factory draft", detail.data)
                self.assertIn(b"Accept into Content Studio", detail.data)
                self.assertNotIn(b"Publish article", detail.data)

    def test_service_has_no_llm_or_publication_routing(self):
        source = Path("app/services/knowledge_draft_generation_service.py").read_text(
            encoding="utf-8",
        ).casefold()
        self.assertNotIn("openai", source)
        self.assertNotIn("gemini", source)
        self.assertNotIn("knowledgepublicationservice", source)


if __name__ == "__main__":
    unittest.main()
