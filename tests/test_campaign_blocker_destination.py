import json
import tempfile
import unittest
from pathlib import Path

from app.services.campaign_blocker_destination_service import CampaignBlockerDestinationService


class CampaignBlockerDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CampaignBlockerDestinationService(self.root)
        self.campaign = {"campaign_id": "KCP-1"}
        self.work = {"work_item_id": "KCW-DNS"}
        self.blocker = {"blocker_type": "workflow_eligibility"}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, directory, filename, value):
        path = self.root / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_exact_claim_plan_is_authoritative_next_workspace(self):
        self.write("claim_planning", "KCPM-1.json", {
            "claim_plan_id": "KCPM-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-DNS", "status": "needs_review"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_claim_planning_detail")
        self.assertEqual(result["route_values"], {"plan_id": "KCPM-1"})

    def test_exact_research_package_is_reused_when_no_claim_plan_exists(self):
        self.write("research", "KRP-1.json", {
            "package_id": "KRP-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-DNS", "status": "approved"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_source_research_detail")

    def test_package_for_another_work_item_is_never_reused(self):
        self.write("claim_planning", "KCPM-1.json", {
            "claim_plan_id": "KCPM-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-OTHER", "status": "needs_review"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_campaign_orchestration_detail")
        self.assertEqual(result["label"], "Continue: Prepare Research")

    def test_ambiguous_current_packages_remain_safely_blocked(self):
        for suffix in ("1", "2"):
            self.write("claim_planning", f"KCPM-{suffix}.json", {
                "claim_plan_id": f"KCPM-{suffix}", "campaign_id": "KCP-1",
                "work_item_id": "KCW-DNS", "status": "needs_review"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertFalse(result["resolved"])
        self.assertIn("Multiple current claim plans", result["reason"])

    def test_rejected_package_is_not_actionable(self):
        self.write("claim_planning", "KCPM-1.json", {
            "claim_plan_id": "KCPM-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-DNS", "status": "rejected"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_campaign_orchestration_detail")

    def test_evidence_workspace_is_preferred_after_research(self):
        self.write("research", "KRP-1.json", {
            "package_id": "KRP-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-DNS", "status": "approved"})
        self.write("evidence_extraction", "KEX-1.json", {
            "extraction_id": "KEX-1", "research_package_id": "KRP-1",
            "status": "needs_review"})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_evidence_extraction_detail")
        self.assertEqual(result["label"], "Review Evidence")

    def test_approved_evidence_exposes_workflow_claim_planning(self):
        self.write("research", "KRP-1.json", {
            "package_id": "KRP-1", "campaign_id": "KCP-1",
            "work_item_id": "KCW-DNS", "status": "approved"})
        self.write("evidence_extraction", "KEX-1.json", {
            "extraction_id": "KEX-1", "research_package_id": "KRP-1",
            "status": "approved", "evidence_units": [
                {"evidence_id": "EVD-1", "review_state": "approved"}]})
        result = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["endpoint"], "knowledge_workflow_claim_planning_prepare")
        self.assertEqual(result["label"], "Plan Workflow Claims")

    def test_unrelated_blocker_has_no_guessed_destination(self):
        result = self.service.resolve(self.campaign, self.work, {"blocker_type": "source_state"})
        self.assertFalse(result["resolved"])
        self.assertIn("no governed internal destination", result["reason"])

    def test_resolver_is_read_only_and_idempotent(self):
        before = list(self.root.rglob("*"))
        first = self.service.resolve(self.campaign, self.work, self.blocker)
        second = self.service.resolve(self.campaign, self.work, self.blocker)
        self.assertEqual(first, second)
        self.assertEqual(before, list(self.root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
