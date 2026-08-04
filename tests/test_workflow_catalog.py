import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class WorkflowCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_home_is_capped_and_links_to_complete_catalog(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Recommended workflows", html)
        self.assertIn("Browse all workflows", html)
        self.assertIn('href="/workflows"', html)
        self.assertLessEqual(html.count("workflow-card-item"), 4)
        self.assertNotIn('id="workflowCategoryFilters"', html)

    def test_catalog_contains_all_workflows_and_filter_controls(self):
        html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn("Browse Workflows", html)
        self.assertIn('id="workflowCategoryFilters"', html)
        self.assertIn('id="workflowFilterSearch"', html)
        self.assertIn('data-workflow-category="favorites"', html)
        self.assertIn('data-workflow-category="recent"', html)
        self.assertIn("workflow_favorites.js", html)
        for title in (
            "Computer Running Slowly",
            "Internet Connection",
            "Printer",
            "Advanced Network Diagnostics",
        ):
            self.assertIn(title, html)

    def test_recent_workflow_is_prioritized_on_home(self):
        record = self.history.start("printer", "Printer", "start")
        html = self.client.get("/").get_data(as_text=True)
        printer_position = html.index("Printer")
        network_position = html.index("Advanced Network Diagnostics")
        self.assertLess(printer_position, network_position)
        self.history.delete(record["id"])

    def test_favorite_toggle_persists_and_prioritizes_home(self):
        added = self.client.post("/api/workflow-favorites/printer")
        self.assertEqual(added.status_code, 200)
        self.assertTrue(added.get_json()["favorite"])
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session["favorite_workflow_ids"], ["printer"])
        catalog = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn('data-workflow-id="printer"', catalog)
        self.assertIn('aria-label="Remove Printer from favorites"', catalog)
        home = self.client.get("/").get_data(as_text=True)
        self.assertLess(home.index("Printer"), home.index("Advanced Network Diagnostics"))
        removed = self.client.post("/api/workflow-favorites/printer")
        self.assertFalse(removed.get_json()["favorite"])

    def test_unknown_favorite_is_rejected(self):
        response = self.client.post("/api/workflow-favorites/not-real")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
