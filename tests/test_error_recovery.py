import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.knowledge.knowledge_base import KnowledgeBase
from app.services.device_profile_service import DeviceProfileError, DeviceProfileService
from app.services.workflow_draft_service import WorkflowDraftError, WorkflowDraftService
from app.services.workflow_publication_service import WorkflowPublicationError, WorkflowPublicationService


class ErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_branded_404_and_api_404_include_request_ids(self):
        page = self.client.get("/not-a-real-page", headers={"X-Request-ID": "known-request"})
        self.assertEqual(page.status_code, 404)
        self.assertEqual(page.headers["X-Request-ID"], "known-request")
        self.assertIn("We couldn&#39;t find that page", page.get_data(as_text=True))
        self.assertIn("known-request", page.get_data(as_text=True))

        api = self.client.get("/api/not-a-real-resource")
        self.assertEqual(api.status_code, 404)
        self.assertFalse(api.get_json()["ok"])
        self.assertEqual(api.get_json()["request_id"], api.headers["X-Request-ID"])

    def test_unhandled_error_is_safe_and_traceable(self):
        with app.test_client() as client, patch.dict(
            app.config,
            {"TESTING": False, "PROPAGATE_EXCEPTIONS": False},
        ), patch("app.app.DeviceProfileService.list", side_effect=RuntimeError("secret internal detail")):
            response = client.get("/device-profiles")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Gnojo hit an unexpected problem", html)
        self.assertNotIn("secret internal detail", html)
        self.assertIn(response.headers["X-Request-ID"], html)

    def test_damaged_draft_is_listed_safely_and_opening_has_recovery_page(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "broken.json").write_text("{not-json", encoding="utf-8")
            service = WorkflowDraftService(directory)
            drafts = service.list_drafts()
            self.assertTrue(drafts[0]["is_damaged"])
            with self.assertRaises(WorkflowDraftError):
                service.get_draft("broken.json")
            with patch("app.app.WorkflowDraftService", return_value=service):
                response = self.client.get("/workflow-editor/broken.json")
            self.assertEqual(response.status_code, 409)
            self.assertIn("Saved data needs attention", response.get_data(as_text=True))

    def test_damaged_profile_publication_and_article_do_not_leak_raw_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_id = "a" * 32
            Path(directory, f"{profile_id}.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(DeviceProfileError):
                DeviceProfileService(directory).get(profile_id)

        with tempfile.TemporaryDirectory() as directory:
            workflow_dir = Path(directory, "broken_workflow"); workflow_dir.mkdir()
            (workflow_dir / "current.json").write_text(json.dumps({"current_version": 1}), encoding="utf-8")
            (workflow_dir / "v0001.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(WorkflowPublicationError):
                WorkflowPublicationService(directory).load_current("broken_workflow")

        with tempfile.TemporaryDirectory() as directory:
            published = Path(directory, "published"); published.mkdir()
            (published / "broken.json").write_text("broken", encoding="utf-8")
            knowledge = KnowledgeBase(); knowledge.knowledge_path = Path(directory); knowledge.published_path = published
            self.assertIsNone(knowledge.load_article("broken"))

    def test_stale_session_is_cleared_on_home(self):
        with self.client.session_transaction() as session:
            session["workflow"] = "deleted_workflow"
            session["current_node"] = "missing"
            session["step"] = 7
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Resume troubleshooting", response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertNotIn("workflow", session)
            self.assertNotIn("current_node", session)


if __name__ == "__main__":
    unittest.main()
