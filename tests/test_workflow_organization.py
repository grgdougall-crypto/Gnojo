import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.search_service import SearchService
from app.services.workflow_draft_service import WorkflowDraftError, WorkflowDraftService
from app.services.workflow_publication_service import WorkflowPublicationService


class WorkflowOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.drafts_temp = tempfile.TemporaryDirectory()
        self.publications_temp = tempfile.TemporaryDirectory()
        self.workflow = {
            "workflow_id": "identity_help", "name": "Identity Help", "description": "Troubleshoot directory permissions.",
            "category": "Servers & Identity", "platform": "Windows", "estimated_steps": 2, "start_node": "start",
            "nodes": {
                "start": {"type": "instruction", "title": "Check access", "instruction": "Review permissions.", "next": "done"},
                "done": {"type": "resolution", "title": "Complete", "message": "Access restored."},
            },
        }
        Path(self.drafts_temp.name, "identity.json").write_text(json.dumps(self.workflow), encoding="utf-8")

    def tearDown(self):
        self.drafts_temp.cleanup()
        self.publications_temp.cleanup()

    def test_settings_persist_category_and_platform(self):
        service = WorkflowDraftService(self.drafts_temp.name)
        updated = service.update_settings("identity.json", {
            "name": "Identity Help", "description": "Troubleshoot directory permissions.",
            "category": "Security", "platform": "Cross-platform", "estimated_steps": 2, "start_node": "start",
        })
        self.assertEqual(updated["category"], "Security")
        self.assertEqual(updated["platform"], "Cross-platform")
        with self.assertRaises(WorkflowDraftError):
            service.update_settings("identity.json", {**updated, "category": "Made Up"})

    def test_published_metadata_drives_cards_filters_and_search(self):
        publications = WorkflowPublicationService(self.publications_temp.name)
        publications.publish(self.workflow, "identity.json")
        with patch("app.app.WorkflowPublicationService", return_value=publications):
            html = app.test_client().get("/workflows").get_data(as_text=True)
        self.assertIn('data-workflow-category="Servers &amp; Identity"', html)
        self.assertIn("workflowFilterSearch", html)
        self.assertIn("Servers &amp; Identity", html)
        self.assertIn("Windows", html)
        self.assertIn("workflow_discovery.js", html)

        service = SearchService()
        service.knowledge.get_published = lambda: []
        service.commands.get_all = lambda: []
        with patch("app.services.search_service.WorkflowPublicationService", return_value=publications):
            results = service.search_all("Windows identity")
        self.assertEqual(results[0].id, "identity_help")
        self.assertEqual(results[0].category, "Servers & Identity")
        self.assertEqual(results[0].difficulty, "Windows")


if __name__ == "__main__":
    unittest.main()
