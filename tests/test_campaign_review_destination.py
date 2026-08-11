import json
import tempfile
import unittest
from pathlib import Path

from app.services.campaign_review_destination_service import CampaignReviewDestinationService
from app.services.knowledge_campaign_orchestration_service import KnowledgeCampaignOrchestrationService


class CampaignReviewDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def article(self, article_id, state="published"):
        folder = self.root / "knowledge_base" / state
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{article_id}.json").write_text(json.dumps({
            "id": article_id, "canonical_id": article_id, "title": article_id.replace("-", " ").title(),
            "review_status": "approved" if state == "published" else "draft",
        }), encoding="utf-8")

    def test_canonical_slug_without_resource_is_not_a_url(self):
        result = CampaignReviewDestinationService(self.root).resolve(
            {"type": "shared_article", "article_id": "missing-article"})
        self.assertFalse(result["resolved"])
        self.assertNotIn("endpoint", result)

    def test_published_article_uses_authoritative_published_route(self):
        self.article("windows-storage-performance")
        result = CampaignReviewDestinationService(self.root).resolve(
            {"type": "shared_article", "article_id": "windows-storage-performance"})
        self.assertEqual((result["owner"], result["endpoint"], result["route_values"]),
                         ("knowledge", "view_published", {"article_id": "windows-storage-performance"}))

    def test_article_draft_uses_authoritative_review_route(self):
        self.article("draft-guide", "drafts")
        result = CampaignReviewDestinationService(self.root).resolve(
            {"type": "shared_article", "article_id": "draft-guide"})
        self.assertEqual(result["endpoint"], "review_draft")

    def test_workflow_reuse_uses_existing_workflow_editor(self):
        folder = self.root / "app" / "workflow_drafts"
        folder.mkdir(parents=True)
        (folder / "printer.json").write_text(json.dumps({"workflow_id": "printer", "name": "Printer"}), encoding="utf-8")
        result = CampaignReviewDestinationService(self.root).resolve(
            {"type": "shared_workflow", "workflow_id": "printer"})
        self.assertEqual(result["endpoint"], "workflow_editor")
        self.assertEqual(result["route_values"], {"filename": "printer.json"})

    def test_legacy_same_area_items_resolve_by_exact_evidence(self):
        opportunities = [
            {"opportunity_id": "storage", "article_id": "windows-storage-performance", "areas": ["internet"], "evidence": ["storage evidence"]},
            {"opportunity_id": "tasks", "article_id": "windows-task-manager-performance", "areas": ["internet"], "evidence": ["task evidence"]},
        ]
        campaign = {"reuse_opportunities": opportunities}
        storage = KnowledgeCampaignOrchestrationService._reuse_for_work(
            campaign, {"area_id": "internet", "evidence": ["storage evidence"]})
        tasks = KnowledgeCampaignOrchestrationService._reuse_for_work(
            campaign, {"area_id": "internet", "evidence": ["task evidence"]})
        self.assertEqual(storage["opportunity_id"], "storage")
        self.assertEqual(tasks["opportunity_id"], "tasks")

    def test_ambiguous_area_without_identity_does_not_select_first_candidate(self):
        campaign = {"reuse_opportunities": [
            {"opportunity_id": "one", "areas": ["internet"]},
            {"opportunity_id": "two", "areas": ["internet"]},
        ]}
        self.assertIsNone(KnowledgeCampaignOrchestrationService._reuse_for_work(
            campaign, {"area_id": "internet"}))

    def test_duplicate_projection_does_not_create_duplicate_card(self):
        service = object.__new__(KnowledgeCampaignOrchestrationService)
        state = {"work_item_id": "same", "gap_id": "gap", "title": "Internet",
                 "stage": "reuse_available", "state": "complete", "package_id": "article",
                 "reuse": {"opportunity_id": "reuse-one"}, "dependencies": [],
                 "action_authority": None, "blocker": None, "stale": False}
        result = service._projection({"campaign_id": "campaign", "objective": "test"}, [state, dict(state)])
        self.assertEqual(len(result["work_item_states"]), 1)

    def test_distinct_reuse_candidates_remain_distinct_despite_legacy_work_id_collision(self):
        service = object.__new__(KnowledgeCampaignOrchestrationService)
        common = {"work_item_id": "legacy-same", "gap_id": "legacy-gap", "title": "Internet",
                  "stage": "reuse_available", "state": "complete", "dependencies": [],
                  "action_authority": None, "blocker": None, "stale": False}
        states = [dict(common, package_id="storage", reuse={"opportunity_id": "storage"}),
                  dict(common, package_id="tasks", reuse={"opportunity_id": "tasks"})]
        result = service._projection({"campaign_id": "campaign", "objective": "test"}, states)
        self.assertEqual([item["package_id"] for item in result["work_item_states"]], ["storage", "tasks"])


if __name__ == "__main__":
    unittest.main()
