import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import AVAILABLE_WORKFLOWS, app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_publication_service import WorkflowPublicationService


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
        self.assertIn("Recommended Workflows", html)
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

    @staticmethod
    def _workflow(workflow_id, name):
        return {
            "workflow_id": workflow_id,
            "name": name,
            "description": f"Current published guidance for {name}.",
            "start_node": "start",
            "nodes": {
                "start": {
                    "type": "instruction", "title": "Start",
                    "instruction": "Inspect the current condition.", "next": "done",
                },
                "done": {"type": "resolution", "title": "Done", "message": "Complete."},
            },
        }

    def test_current_publications_override_fallback_and_catalog_renders_all_thirteen(self):
        publication_root = Path(self.temporary.name) / "publications"
        publications = WorkflowPublicationService(publication_root)
        identities = list(AVAILABLE_WORKFLOWS) + [f"published_{index}" for index in range(5)]
        for workflow_id in identities:
            name = "Current Internet Guidance" if workflow_id == "internet" else workflow_id.replace("_", " ").title()
            publications.publish(self._workflow(workflow_id, name), f"{workflow_id}.json")
        before = {
            path.relative_to(publication_root): path.read_bytes()
            for path in publication_root.rglob("*") if path.is_file()
        }

        with patch("app.app.WorkflowPublicationService", return_value=publications):
            catalog = available_workflows()
            response = self.client.get("/workflows")
            home = self.client.get("/")

        self.assertEqual(len(catalog), 13)
        self.assertEqual(catalog["internet"]["name"], "Current Internet Guidance")
        self.assertTrue(all(item["source"] == "published" for item in catalog.values()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True).count("workflow-card-item"), 13)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Explore all 13 workflows", home.get_data(as_text=True))
        after = {
            path.relative_to(publication_root): path.read_bytes()
            for path in publication_root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_missing_runtime_publications_use_fallback_without_creating_storage(self):
        publication_root = Path(self.temporary.name) / "not-created"
        publications = WorkflowPublicationService(publication_root)
        with patch("app.app.WorkflowPublicationService", return_value=publications):
            catalog = available_workflows()
        self.assertEqual(set(catalog), set(AVAILABLE_WORKFLOWS))
        self.assertTrue(all(item["source"] == "built_in" for item in catalog.values()))
        self.assertFalse(publication_root.exists())

    def test_malformed_current_publication_fails_closed_instead_of_showing_fallback(self):
        publication_root = Path(self.temporary.name) / "damaged-publications"
        workflow_root = publication_root / "internet"
        workflow_root.mkdir(parents=True)
        (workflow_root / "current.json").write_text('{"current_version": 1}', encoding="utf-8")
        (workflow_root / "v0001.json").write_text("{not-json", encoding="utf-8")
        publications = WorkflowPublicationService(publication_root)
        with patch("app.app.WorkflowPublicationService", return_value=publications):
            response = self.client.get("/workflows")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Saved data needs attention", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
