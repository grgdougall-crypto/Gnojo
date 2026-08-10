import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.knowledge.article_schema import create_article_template
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_draft_generation_service import KnowledgeDraftGenerationService
from app.services.knowledge_draft_refinement_service import (
    KnowledgeDraftRefinementError,
    KnowledgeDraftRefinementService,
)


class KnowledgeDraftRefinementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in (
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
            "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/archive",
            "knowledge_base/commands", "knowledge_base/scripts",
        ):
            (self.root / directory).mkdir(parents=True)
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"schema_version": "1.0", "domains": [{
            "id": "windows-connectivity", "title": "Windows Connectivity",
            "category": "Networking", "platforms": ["Windows"],
            "areas": [{"id": "vpn", "title": "VPN", "terms": ["vpn", "connectivity"]}],
        }]}), encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({
            "schema_version": "1.0", "tiers": [{"tier": 1, "label": "First-party",
            "publishers": [{"name": "Microsoft", "domains": ["learn.microsoft.com"]}]}],
            "research_targets": [{"platform": "Windows", "vendor": "Microsoft",
            "search_provider": "microsoft_learn", "domains": ["learn.microsoft.com"]}],
        }), encoding="utf-8")
        self.campaign_root = self.root / "campaigns"
        planner = KnowledgeCoveragePlannerService(self.root, self.campaign_root, self.taxonomy)
        campaign = planner.analyze(planner.create(
            title="Windows Connectivity Pilot", domain_id="windows-connectivity",
            objective="Create trusted Windows connectivity guidance.",
        )["campaign_id"])
        self.gap = next(item for item in campaign["gaps"] if item["gap_type"] == "missing_article")
        self.work = next(item for item in campaign["work_items"] if item["gap_id"] == self.gap["gap_id"])
        self.repository = KnowledgeRepository(self.root / "knowledge_base")
        self.generation = KnowledgeDraftGenerationService(
            self.root, self.campaign_root, self.taxonomy, self.policy, self.repository,
        )
        self.service = KnowledgeDraftRefinementService(self.generation)

    def tearDown(self):
        self.temporary.cleanup()

    def approve_evidence(self, *, structured=True):
        package_root = self.campaign_root / "research"
        package_root.mkdir(parents=True, exist_ok=True)
        candidate = {
            "source_candidate_id": "KSC-MICROSOFT", "review_state": "selected",
            "topic_relevant": True,
            "canonical_url": "https://learn.microsoft.com/windows/security/vpn/",
            "page_title": "VPN technical guide", "authority_tier": 1,
            "provenance": {"content_digest": "approved-digest"},
        }
        if structured:
            candidate["approved_evidence"] = [
                {"section": "symptoms", "text": "The VPN cannot establish a connection."},
                {"section": "procedure", "text": "Open the approved VPN client and record its status."},
                {"section": "verification", "text": "Confirm the client reports a connected state."},
            ]
        package = {
            "schema_version": "1.0", "package_id": "KRP-AAAAAAAAAAAA",
            "campaign_id": self.work["campaign_id"], "gap_id": "KCG-SOURCE",
            "work_item_id": "KCW-SOURCE", "target_coverage_area": "vpn",
            "status": "approved", "created_at": "2026-08-10T00:00:00+00:00",
            "selected_sources": ["KSC-MICROSOFT"], "candidate_sources": [candidate],
        }
        (package_root / "KRP-AAAAAAAAAAAA.json").write_text(json.dumps(package), encoding="utf-8")
        return package_root / "KRP-AAAAAAAAAAAA.json"

    def prepare(self):
        return self.generation.prepare(
            self.work["campaign_id"], self.gap["gap_id"], self.work["work_item_id"],
        )

    def test_refinement_is_explicit_deterministic_traceable_and_idempotent(self):
        self.approve_evidence()
        package = self.prepare()
        self.assertNotIn("refinement", package)
        first = self.service.refine(package["package_id"])
        second = self.service.refine(package["package_id"])
        self.assertEqual(first["generation_status"], "ready_for_review")
        self.assertEqual(first["refinement"]["method"], "deterministic-evidence-refiner-v1")
        self.assertEqual(first["draft_preview"]["checklist"],
                         ["Open the approved VPN client and record its status."])
        self.assertTrue(all(item["evidence_ids"] for item in first["refinement"]["claim_traceability"]))
        self.assertEqual(first["history"], second["history"])
        self.assertEqual(first["refinement"]["input_fingerprint"],
                         second["refinement"]["input_fingerprint"])

    def test_approved_url_without_structured_claims_needs_evidence(self):
        self.approve_evidence(structured=False)
        refined = self.service.refine(self.prepare()["package_id"])
        self.assertEqual(refined["generation_status"], "needs_evidence")
        self.assertIn("procedure", refined["refinement"]["validation"]["required_incomplete"])
        self.assertIn("verification", refined["refinement"]["validation"]["required_incomplete"])
        self.assertNotIn("https://", " ".join(refined["draft_preview"]["checklist"]))

    def test_human_approved_phase_five_evidence_satisfies_refinement_boundary(self):
        self.approve_evidence(structured=False)
        package = self.prepare()
        extraction_root = self.campaign_root / "evidence_extraction"
        extraction_root.mkdir(parents=True, exist_ok=True)
        units = []
        for index, (section, text) in enumerate((
            ("procedure", "Open the approved VPN client and record its status."),
            ("verification", "Confirm the client reports a connected state."),
        )):
            units.append({
                "evidence_id": f"EVD-AAAAAAA{index:05d}", "evidence_type": section,
                "normalized_claim": text, "review_state": "approved",
                "source_title": "VPN technical guide",
                "source_url": "https://learn.microsoft.com/windows/security/vpn/",
                "publisher": "Microsoft", "fingerprint": f"digest-{index}",
                "provenance": {"research_package_id": "KRP-AAAAAAAAAAAA",
                               "source_candidate_id": "KSC-MICROSOFT"},
            })
        (extraction_root / "KEX-AAAAAAAAAAAA.json").write_text(json.dumps({
            "schema_version": "1.0", "extraction_id": "KEX-AAAAAAAAAAAA",
            "research_package_id": "KRP-AAAAAAAAAAAA", "status": "approved",
            "created_at": "now", "evidence_units": units,
        }), encoding="utf-8")
        refined = self.service.refine(package["package_id"])
        self.assertEqual(refined["generation_status"], "ready_for_review")
        evidence = refined["refinement"]["evidence_records"]
        self.assertTrue(any(item["kind"] == "approved_extracted_evidence" for item in evidence))
        self.assertEqual(refined["draft_preview"]["checklist"], [units[0]["normalized_claim"]])

    def test_unapproved_research_is_excluded_after_phase_three(self):
        path = self.approve_evidence()
        package = self.prepare()
        research = json.loads(path.read_text(encoding="utf-8"))
        research["status"] = "rejected"
        path.write_text(json.dumps(research), encoding="utf-8")
        refined = self.service.refine(package["package_id"])
        self.assertEqual(refined["generation_status"], "needs_evidence")
        self.assertFalse(any(item["kind"] == "approved_source"
                             for item in refined["refinement"]["evidence_records"]))

    def test_material_unsupported_guidance_blocks_review(self):
        self.approve_evidence()
        package = self.prepare()
        package["draft_preview"]["checklist"] = ["Disable the firewall permanently."]
        self.generation._save(package)
        refined = self.service.refine(package["package_id"])
        self.assertEqual(refined["generation_status"], "needs_revision")
        self.assertEqual(refined["refinement"]["validation"]["material_unsupported_count"], 1)

    def test_optional_sections_are_explicitly_not_applicable(self):
        self.approve_evidence()
        refined = self.service.refine(self.prepare()["package_id"])
        statuses = {item["section"]: item["status"]
                    for item in refined["refinement"]["section_coverage"]}
        self.assertEqual(statuses["safety"], "not_applicable")
        self.assertEqual(statuses["commands"], "not_applicable")
        self.assertEqual(statuses["sources"], "supported")

    def test_approved_canonical_article_is_reused_with_traceability(self):
        self.approve_evidence()
        package = self.prepare()
        canonical = create_article_template()
        canonical.update({
            "id": "approved-vpn-foundation",
            "title": "Approved VPN Foundation",
            "overview": "Use the approved VPN client for organization-managed access.",
            "category": "Networking",
            "checklist": ["Confirm the approved VPN client is installed."],
            "review": {"status": "approved", "reviewed_by": "Reviewer",
                       "reviewed_at": "2026-08-10", "notes": []},
        })
        self.repository.save_published(canonical)
        package["existing_assets_considered"].append({
            "content_type": "article", "identifier": canonical["id"],
            "title": canonical["title"], "state": "published", "areas": ["vpn"],
        })
        self.generation._save(package)

        refined = self.service.refine(package["package_id"])

        reused = refined["refinement"]["reusable_knowledge"]
        self.assertEqual([item["article_id"] for item in reused], [canonical["id"]])
        self.assertIn(canonical["title"], refined["draft_preview"]["related_topics"])
        canonical_evidence = [item for item in refined["refinement"]["evidence_records"]
                              if item["kind"] == "canonical_gnojo_article"]
        self.assertEqual(canonical_evidence[0]["article_id"], canonical["id"])

    def test_existing_identity_blocks_duplicate_before_handoff(self):
        self.approve_evidence()
        package = self.prepare()
        existing = deepcopy(package["draft_preview"])
        existing["review"] = {"status": "approved", "reviewed_by": "Reviewer",
                              "reviewed_at": "2026-08-10", "notes": []}
        self.repository.save_published(existing)
        refined = self.service.refine(package["package_id"])
        self.assertEqual(refined["generation_status"], "needs_revision")
        self.assertEqual(refined["refinement"]["validation"]["identity_conflict"], existing["id"])
        self.assertEqual(self.repository.get_drafts(), [])

    def test_refined_handoff_preserves_metadata_and_never_publishes(self):
        self.approve_evidence()
        refined = self.service.refine(self.prepare()["package_id"])
        accepted = self.generation.accept_into_content_studio(refined["package_id"])
        self.assertEqual(accepted["generation_status"], "accepted_into_content_studio")
        saved = self.repository.get_drafts()[0]
        self.assertEqual(saved["refinement"]["package_id"], refined["package_id"])
        self.assertEqual(saved["knowledge_factory"]["refinement_method"],
                         "deterministic-evidence-refiner-v1")
        self.assertEqual(self.repository.get_published(), [])

    def test_ineligible_lifecycle_and_missing_preview_are_rejected(self):
        package = self.prepare()
        with self.assertRaisesRegex(KnowledgeDraftRefinementError, "Phase 3 draft"):
            self.service.refine(package["package_id"])
        self.approve_evidence()
        # A distinct package is unnecessary: updating the blocked package would violate
        # Phase 3 idempotence, so lifecycle eligibility is exercised directly.
        package["draft_preview"] = create_article_template()
        package["generation_status"] = "rejected"
        self.generation._save(package)
        with self.assertRaisesRegex(KnowledgeDraftRefinementError, "ineligible"):
            self.service.refine(package["package_id"])

    def test_refinement_does_not_research_publish_or_mutate_protected_state(self):
        self.approve_evidence()
        package = self.prepare()
        protected = []
        for name in ("app/decision_trees/sentinel.json", "app/workflow_publications/sentinel.json",
                     "curation_memory/memory.json", "curation_runs/latest.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8")
            protected.append(path)
        before = {path: path.read_bytes() for path in protected}
        with patch.object(self.generation.research, "run", side_effect=AssertionError("research called")):
            self.service.refine(package["package_id"])
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        self.assertEqual(self.repository.get_published(), [])

    def test_ui_exposes_human_initiated_refinement_and_governance_results(self):
        self.approve_evidence()
        package = self.prepare()
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeDraftGenerationService", return_value=self.generation), \
             patch("app.app.KnowledgeDraftRefinementService", return_value=self.service):
            with flask_app.test_client() as client:
                detail = client.get(f"/curator/growth/draft-generation/{package['package_id']}")
                self.assertIn(b"Refine Draft", detail.data)
                response = client.post(
                    f"/curator/growth/draft-generation/{package['package_id']}/refine"
                )
                self.assertEqual(response.status_code, 302)
                detail = client.get(f"/curator/growth/draft-generation/{package['package_id']}")
                self.assertIn(b"Section Coverage", detail.data)
                self.assertIn(b"Evidence Traceability", detail.data)
                self.assertIn(b"Re-run Refinement", detail.data)
                self.assertNotIn(b"Publish article", detail.data)

    def test_service_has_no_network_or_llm_routing(self):
        source = Path("app/services/knowledge_draft_refinement_service.py").read_text(
            encoding="utf-8",
        ).casefold()
        for forbidden in ("openai", "gemini", "requests", "urlopen", "web.run"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
