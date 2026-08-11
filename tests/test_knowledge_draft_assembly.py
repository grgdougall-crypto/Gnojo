import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from app.app import app as flask_app
from app.knowledge.article_schema import create_article_template
from app.services.knowledge_draft_assembly_service import (
    KnowledgeDraftAssemblyError,
    KnowledgeDraftAssemblyService,
)
from tests import test_knowledge_claim_planning as claim_planning_tests


class KnowledgeDraftAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = claim_planning_tests.KnowledgeClaimPlanningTests(
            methodName="test_stable_plan_claim_identity_and_idempotent_replanning"
        )
        self.fixture.setUp()
        self.service = KnowledgeDraftAssemblyService(
            self.fixture.generation, self.fixture.campaign_root
        )

    def tearDown(self):
        self.fixture.tearDown()

    def _ready(self, units=None):
        plan = self.fixture._approve_all(self.fixture._planned(units))
        self.assertEqual(plan["status"], "ready_for_drafting")
        return plan

    def _assemble(self, units=None):
        return self.service.assemble(self._ready(units)["claim_plan_id"])

    def test_explicit_human_initiation_and_ready_plan_are_required(self):
        self.assertEqual(self.service.list_for_kdg(self.fixture.package["package_id"]), [])
        plan = self.fixture._planned()
        self.assertFalse(self.service.is_eligible(plan["claim_plan_id"]))
        with self.assertRaisesRegex(KnowledgeDraftAssemblyError, "ready-for-drafting"):
            self.service.assemble(plan["claim_plan_id"])
        self.assertEqual(self.service.list_for_kdg(self.fixture.package["package_id"]), [])

    def test_only_approved_current_claims_are_eligible(self):
        plan = self.fixture._planned()
        first = plan["claims"][0]
        self.fixture.service.review_claim(plan["claim_plan_id"], first["claim_id"], "approved")
        with self.assertRaises(KnowledgeDraftAssemblyError):
            self.service.assemble(plan["claim_plan_id"])
        self.fixture.service.review_claim(plan["claim_plan_id"], plan["claims"][1]["claim_id"], "rejected")
        with self.assertRaises(KnowledgeDraftAssemblyError):
            self.service.assemble(plan["claim_plan_id"])

    def test_assembly_is_deterministic_idempotent_and_retains_provenance(self):
        plan = self._ready()
        first = self.service.assemble(plan["claim_plan_id"])
        second = self.service.assemble(plan["claim_plan_id"])
        self.assertRegex(first["assembly_id"], r"^KASM-[A-F0-9]{12}$")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ready_for_review")
        self.assertEqual(first["approved_claim_ids"], [c["claim_id"] for c in plan["claims"]])
        self.assertEqual(set(first["supporting_evidence_ids"]), set(plan["approved_evidence_ids"]))
        factory = first["assembled_content"]["knowledge_factory"]
        self.assertEqual(factory["campaign_id"], self.fixture.work["campaign_id"])
        self.assertEqual(factory["gap_id"], self.fixture.gap["gap_id"])
        self.assertEqual(factory["work_item_id"], self.fixture.work["work_item_id"])
        self.assertEqual(factory["claim_plan_id"], plan["claim_plan_id"])
        self.assertEqual(factory["assembly_id"], first["assembly_id"])

    def test_section_order_procedure_order_and_non_applicable_sections(self):
        units = [
            self.fixture._unit("EVD-PROCEDURE01", "procedure", "Open Settings."),
            self.fixture._unit("EVD-PROCEDURE02", "procedure", "Select Network and Internet."),
            self.fixture._unit("EVD-VERIFY00001", "verification", "Verify Connected is displayed."),
        ]
        assembly = self._assemble(units)
        names = [item["section"] for item in assembly["section_map"]]
        self.assertEqual(names, [name for name in self.service.SECTION_ORDER if name in names])
        procedure = next(item for item in assembly["section_map"] if item["section"] == "procedure")
        self.assertEqual(procedure["content"], ["Open Settings.", "Select Network and Internet."])
        self.assertNotIn("commands", assembly["assembled_content"]["assembly_sections"])

    def test_caution_authorization_and_command_syntax_are_preserved(self):
        units = [
            self.fixture._unit("EVD-PROCEDURE01", "procedure", "Restart the VPN client."),
            self.fixture._unit("EVD-SAFETY00001", "safety", "Save work before restarting."),
            self.fixture._unit("EVD-PRECOND0001", "preconditions", "Administrator approval is required."),
            self.fixture._unit("EVD-COMMAND0001", "commands", "Get-NetAdapter | Format-Table -AutoSize"),
            self.fixture._unit("EVD-VERIFY00001", "verification", "Verify Connected is displayed."),
        ]
        assembly = self._assemble(units)
        sections = assembly["assembled_content"]["assembly_sections"]
        self.assertEqual(sections["safety"], ["Save work before restarting."])
        self.assertEqual(sections["prerequisites"], ["Administrator approval is required."])
        self.assertEqual(sections["commands"], ["Get-NetAdapter | Format-Table -AutoSize"])
        self.assertEqual(assembly["assembled_content"]["commands"][0]["command"],
                         "Get-NetAdapter | Format-Table -AutoSize")
        self.assertFalse(any(item["level"] == "error" for item in assembly["validation_results"]))

    def test_state_changing_procedure_without_safety_fails_validation(self):
        assembly = self._assemble([
            self.fixture._unit("EVD-PROCEDURE01", "procedure", "Restart the VPN client."),
            self.fixture._unit("EVD-VERIFY00001", "verification", "Verify Connected is displayed."),
        ])
        self.assertEqual(assembly["status"], "needs_revision")
        self.assertTrue(any(item["check"] == "safety_authorization" and item["level"] == "error"
                            for item in assembly["validation_results"]))

    def test_required_verification_cannot_reach_ready_for_review_when_missing(self):
        plan = self._ready()
        path = self.fixture.campaign_root / "claim_planning" / f"{plan['claim_plan_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        verification_ids = {item["claim_id"] for item in raw["claims"]
                            if item["section"] == "verification"}
        raw["claims"] = [item for item in raw["claims"]
                         if item["claim_id"] not in verification_ids]
        verification = next(item for item in raw["sections"] if item["section"] == "verification")
        verification["claim_ids"] = []
        verification["evidence_ids"] = []
        path.write_text(json.dumps(raw), encoding="utf-8")
        assembly = self.service.assemble(plan["claim_plan_id"])
        self.assertEqual(assembly["status"], "needs_revision")
        self.assertTrue(any(item["check"] == "verification" and item["level"] == "error"
                            for item in assembly["validation_results"]))

    def test_source_attribution_uses_only_supporting_provenance_and_deduplicates(self):
        assembly = self._assemble()
        self.assertEqual(assembly["assembled_content"]["sources"], [{
            "title": "VPN guide", "url": "https://learn.microsoft.com/vpn"
        }])
        self.assertEqual(len(assembly["source_provenance"]), 1)
        self.assertEqual(set(assembly["source_provenance"][0]["claim_ids"]),
                         set(assembly["approved_claim_ids"]))

    def test_approved_canonical_reuse_is_referenced_not_cloned(self):
        article = create_article_template()
        article.update({"id": "shared-vpn-verification", "canonical_id": "shared-vpn-verification",
            "title": "Shared VPN Verification", "category": "Networking", "overview": "Reusable.",
            "sources": [{"title": "Official", "url": "https://learn.microsoft.com/vpn"}],
            "generation": {"provider": "Human", "model": "manual", "generated_at": "now"},
            "review": {"status": "approved", "reviewed_by": "Human", "reviewed_at": "now", "notes": []}})
        self.fixture.repository.save_draft(article)
        package = self.fixture.generation.get(self.fixture.package["package_id"])
        package["existing_assets_considered"].append({"content_type": "article",
            "identifier": article["id"], "title": article["title"], "state": "draft"})
        self.fixture.generation._save(package)
        plan = self.fixture._planned()
        path = self.fixture.campaign_root / "claim_planning" / f"{plan['claim_plan_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["canonical_reuse"] = [{"article_id": article["id"], "title": article["title"],
            "decision": "approved_reuse", "traceable": True}]
        path.write_text(json.dumps(raw), encoding="utf-8")
        plan = self.fixture._approve_all(self.fixture.service.get(plan["claim_plan_id"]))
        assembly = self.service.assemble(plan["claim_plan_id"])
        self.assertEqual(assembly["assembled_content"]["related_topics"], [article["id"]])
        self.assertEqual(assembly["assembled_content"]["knowledge_factory"]["canonical_reuse_ids"],
                         [article["id"]])
        self.assertNotEqual(assembly["assembled_content"]["overview"], article["overview"])

    def test_published_canonical_identity_blocks_duplicate_assembly(self):
        plan = self._ready()
        article = create_article_template()
        article.update({"id": self.fixture.package["canonical_identity"],
            "canonical_id": self.fixture.package["canonical_identity"],
            "title": self.fixture.package["proposed_title"], "category": "Networking",
            "overview": "Canonical guidance.",
            "sources": [{"title": "Official", "url": "https://learn.microsoft.com/vpn"}],
            "generation": {"provider": "Human", "model": "manual", "generated_at": "now"},
            "review": {"status": "approved", "reviewed_by": "Human", "reviewed_at": "now", "notes": []}})
        self.fixture.repository.save_published(article)
        with self.assertRaisesRegex(KnowledgeDraftAssemblyError, "canonical published article"):
            self.service.assemble(plan["claim_plan_id"])

    def test_stale_evidence_blocks_assembly_and_marks_existing_assembly_stale(self):
        plan = self._ready()
        assembly = self.service.assemble(plan["claim_plan_id"])
        path = self.fixture.campaign_root / "evidence_extraction" / "KEX-AAAAAAAAAAAA.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["status"] = "needs_refresh"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(KnowledgeDraftAssemblyError, "ready-for-drafting"):
            self.service.assemble(plan["claim_plan_id"])
        self.assertEqual(self.service.get(assembly["assembly_id"])["status"], "stale")

    def test_changed_approved_claim_requires_explicit_reassembly_and_preserves_revision(self):
        plan = self._ready()
        first = self.service.assemble(plan["claim_plan_id"])
        path = self.fixture.campaign_root / "claim_planning" / f"{plan['claim_plan_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["claims"][0]["normalized_claim"] += " Record the result."
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.service.get(first["assembly_id"])["status"], "stale")
        second = self.service.assemble(plan["claim_plan_id"])
        self.assertEqual(second["assembly_id"], first["assembly_id"])
        self.assertEqual(len(second["revisions"]), 1)
        self.assertNotEqual(second["fingerprint"], first["fingerprint"])

    def test_content_studio_handoff_is_explicit_idempotent_and_never_publishes(self):
        assembly = self._assemble()
        published_before = list((self.fixture.root / "knowledge_base/published").glob("*.json"))
        first = self.service.handoff(assembly["assembly_id"])
        second = self.service.handoff(assembly["assembly_id"])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "handed_off")
        self.assertTrue((self.fixture.root / "knowledge_base/drafts" /
                         f"{first['content_studio_article_id']}.json").exists())
        self.assertEqual(list((self.fixture.root / "knowledge_base/published").glob("*.json")),
                         published_before)

    def test_routes_expose_assemble_review_and_handoff_without_automatic_action(self):
        plan = self._ready()
        with flask_app.test_client() as client, \
             patch("app.app.KnowledgeClaimPlanningService", return_value=self.fixture.service), \
             patch("app.app.KnowledgeDraftAssemblyService", return_value=self.service):
            detail = client.get(f"/curator/growth/claim-planning/{plan['claim_plan_id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn(b"Assemble Draft", detail.data)
            self.assertEqual(self.service.list_for_kdg(self.fixture.package["package_id"]), [])
            response = client.post(
                f"/curator/growth/claim-planning/{plan['claim_plan_id']}/assemble"
            )
            self.assertEqual(response.status_code, 302)
            assembly = self.service.list_for_kdg(self.fixture.package["package_id"])[0]
            page = client.get(f"/curator/growth/draft-assembly/{assembly['assembly_id']}")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Send to Content Studio", page.data)
            self.assertIn(b"Approved authority only", page.data)

    def test_assembly_performs_no_research_or_unrelated_repository_mutation(self):
        plan = self._ready()
        research_before = deepcopy(json.loads((self.fixture.campaign_root / "research" /
                                               f"{self.fixture.research_id}.json").read_text()))
        with patch.object(self.fixture.generation, "research", create=True) as research:
            self.service.assemble(plan["claim_plan_id"])
            research.assert_not_called()
        research_after = json.loads((self.fixture.campaign_root / "research" /
                                     f"{self.fixture.research_id}.json").read_text())
        self.assertEqual(research_before, research_after)
        self.assertEqual(list((self.fixture.root / "knowledge_base/published").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
