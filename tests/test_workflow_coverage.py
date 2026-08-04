import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.workflow_coverage_service import WorkflowCoverageService
from app.services.workflow_draft_service import WorkflowDraftService


def coverage_workflow():
    return {
        "workflow_id": "coverage_test",
        "name": "Coverage Test",
        "category": "Desktop Support",
        "platform": "Windows",
        "estimated_steps": 2,
        "start_node": "check_step",
        "nodes": {
            "check_step": {
                "type": "instruction",
                "title": "Check Startup Applications",
                "instruction": "Open Task Manager and review Startup apps.",
                "next": "done",
            },
            "done": {"type": "resolution", "title": "Complete", "message": "Finished."},
        },
    }


class WorkflowCoverageServiceTests(unittest.TestCase):
    def test_help_text_is_contextual_and_article_is_valid(self):
        workflow = coverage_workflow()
        node = workflow["nodes"]["check_step"]
        service = WorkflowCoverageService()
        help_text = service.generate_help_text(node)
        self.assertIn("check startup applications", help_text.lower())
        self.assertIn("avoid changing unrelated settings", help_text)
        article = service.create_article_draft(workflow, "check_step", node)
        self.assertEqual(article["id"], "coverage-test-check-step")
        self.assertEqual(ArticleValidator.validate(article), [])
        self.assertEqual(article["review"]["status"], "draft")
        self.assertEqual(article["generation"]["provider"], "Gnojo Coverage Assistant")

    def test_article_generation_rejects_non_instruction_node(self):
        workflow = coverage_workflow()
        with self.assertRaises(ValueError):
            WorkflowCoverageService().create_article_draft(
                workflow, "done", workflow["nodes"]["done"]
            )


class WorkflowCoverageEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.drafts = WorkflowDraftService(root / "workflow_drafts")
        self.repository = KnowledgeRepository(root / "knowledge_base")
        self.filename = self.drafts.save_draft(coverage_workflow())
        self.draft_patch = patch("app.app.WorkflowDraftService", return_value=self.drafts)
        self.repository_patch = patch("app.app.knowledge_repository", self.repository)
        self.draft_patch.start()
        self.repository_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.repository_patch.stop()
        self.draft_patch.stop()
        self.temporary.cleanup()

    def test_help_text_endpoint_saves_normalized_node(self):
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/help-text"
        )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["ok"])
        saved = self.drafts.get_draft(self.filename)
        self.assertEqual(saved["nodes"]["check_step"]["help_text"], result["help_text"])

    def test_article_endpoint_creates_draft_and_links_node(self):
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/article"
        )
        self.assertEqual(response.status_code, 201)
        result = response.get_json()
        self.assertEqual(result["article_id"], "coverage-test-check-step")
        self.assertEqual(result["review_url"], "/knowledge/drafts/coverage-test-check-step")
        article = self.repository.get_draft(result["article_id"])
        self.assertEqual(article["title"], "How to Check Startup Applications")
        workflow = self.drafts.get_draft(self.filename)
        self.assertEqual(
            workflow["nodes"]["check_step"]["knowledge_article"],
            "coverage-test-check-step",
        )
        repeated = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/article"
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.get_json()["created"])

    def test_editor_renders_coverage_controls(self):
        html = self.client.get(f"/workflow-editor/{self.filename}").get_data(as_text=True)
        self.assertIn("Content Coverage Assistant", html)
        self.assertIn("generateHelpTextButton", html)
        self.assertIn("createArticleDraftButton", html)
        self.assertIn("data-help-text-url", html)

    def test_stale_editor_node_returns_refresh_guidance(self):
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/old_node_id/coverage/help-text"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Refresh the Workflow Designer", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
