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

    def test_builtin_workflow_can_be_copied_without_changing_original(self):
        drafts = WorkflowDraftService(self.drafts_temp.name)
        original_path = Path("app/decision_trees/internet.json")
        original_content = original_path.read_text(encoding="utf-8")

        with patch("app.app.WorkflowDraftService", return_value=drafts):
            studio = app.test_client().get("/workflow-studio")
            html = studio.get_data(as_text=True)
            self.assertIn("Built-in Workflows", html)
            self.assertIn("Create Editable Copy", html)
            self.assertIn(
                'aria-label="Create editable copy of Internet Connection"',
                html,
            )

            response = app.test_client().post(
                "/workflow-studio/built-ins/internet/copy"
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/workflow-editor/internet.json"))
        copied = drafts.get_draft("internet.json")
        self.assertEqual(copied["workflow_id"], "internet")
        self.assertEqual(copied["draft_origin"]["type"], "built_in")
        self.assertEqual(copied["status"], "Editable Copy")
        self.assertEqual(original_path.read_text(encoding="utf-8"), original_content)

    def test_existing_editable_copy_is_opened_instead_of_overwritten(self):
        drafts = WorkflowDraftService(self.drafts_temp.name)
        workflow = {
            "workflow_id": "internet",
            "name": "My Reviewed Internet Copy",
            "estimated_steps": 1,
            "start_node": "done",
            "nodes": {
                "done": {"type": "resolution", "title": "Complete", "message": "Done."}
            },
        }
        drafts.save_draft(workflow)

        with patch("app.app.WorkflowDraftService", return_value=drafts):
            response = app.test_client().post(
                "/workflow-studio/built-ins/internet/copy"
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            drafts.get_draft("internet.json")["name"],
            "My Reviewed Internet Copy",
        )

    def test_studio_distinguishes_static_and_branch_aware_progress_and_names_actions(self):
        drafts = WorkflowDraftService(self.drafts_temp.name)
        branch_aware = {
            **self.workflow,
            "workflow_id": "adaptive_help",
            "name": "Adaptive Help",
            "progress_mode": "branch_aware",
            "estimated_steps": 99,
        }
        drafts.save_draft(branch_aware)

        listed = {item["workflow_id"]: item for item in drafts.list_drafts()}
        self.assertEqual(listed["identity_help"]["progress_mode"], "static")
        self.assertEqual(listed["adaptive_help"]["progress_mode"], "branch_aware")

        with patch("app.app.WorkflowDraftService", return_value=drafts):
            response = app.test_client().get("/workflow-studio")
        html = response.get_data(as_text=True)
        self.assertIn("2 planned steps", html)
        self.assertIn("Branch-aware progress", html)
        self.assertIn("Path length adapts to the selected route.", html)
        self.assertNotIn("99 planned steps", html)
        self.assertIn('aria-label="Open Identity Help in Workflow Designer"', html)
        self.assertIn('href="/workflow-editor/identity.json"', html)
        self.assertIn("Open in Designer", html)

    def test_existing_builtin_copy_has_specific_accessible_open_name(self):
        drafts = WorkflowDraftService(self.drafts_temp.name)
        internet = json.loads(Path("app/decision_trees/internet.json").read_text(encoding="utf-8"))
        drafts.save_draft(internet)
        with patch("app.app.WorkflowDraftService", return_value=drafts):
            html = app.test_client().get("/workflow-studio").get_data(as_text=True)
        self.assertIn(
            'aria-label="Open editable copy of Internet Connection"',
            html,
        )
        self.assertIn('href="/workflow-editor/internet.json"', html)


if __name__ == "__main__":
    unittest.main()
