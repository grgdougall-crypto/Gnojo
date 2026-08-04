import tempfile
import unittest
from unittest.mock import patch

from app.app import app
from app.services.workflow_publication_service import WorkflowPublicationService


def versioned_workflow(message="Version one result"):
    return {
        "workflow_id": "session_test", "name": "Session Test", "category": "Networking", "platform": "Cross-platform",
        "estimated_steps": 2, "start_node": "start",
        "nodes": {
            "start": {"type": "question", "title": "Start", "question": "Continue?", "answers": {"yes": {"label": "Yes", "next": "done"}}},
            "done": {"type": "resolution", "title": "Complete", "message": message},
        },
    }


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.publications = WorkflowPublicationService(self.temp.name)
        self.publications.publish(versioned_workflow(), "session.json", "Version one")
        self.client = app.test_client()
        self.patch = patch("app.app.WorkflowPublicationService", return_value=self.publications)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_home_preserves_and_resumes_active_progress(self):
        self.client.get("/wizard?workflow=session_test")
        home = self.client.get("/")
        html = home.get_data(as_text=True)
        self.assertIn("Continue Session Test", html)
        self.assertIn("Resume troubleshooting", html)
        self.assertIn("published version 1", html)
        resumed = self.client.get("/wizard?workflow=session_test&resume=1")
        self.assertIn("Continue?", resumed.get_data(as_text=True))

    def test_starting_again_requires_recovery_decision(self):
        self.client.get("/wizard?workflow=session_test")
        recovery = self.client.get("/wizard?workflow=session_test")
        self.assertIn("Continue where you left off?", recovery.get_data(as_text=True))
        restarted = self.client.get("/wizard?workflow=session_test&restart=1")
        self.assertIn("Continue?", restarted.get_data(as_text=True))

    def test_active_session_is_pinned_to_original_publication(self):
        self.client.get("/wizard?workflow=session_test")
        self.publications.publish(versioned_workflow("Version two result"), "session.json", "Version two")
        completed = self.client.post("/wizard", data={"answer": "yes"}, follow_redirects=True)
        html = completed.get_data(as_text=True)
        self.assertIn("Version one result", html)
        self.assertNotIn("Version two result", html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow_version"], 1)
            self.assertTrue(session["workflow_complete"])
        home = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Resume troubleshooting", home)

    def test_different_workflow_warns_before_replacing_progress(self):
        self.client.get("/wizard?workflow=session_test")
        warning = self.client.get("/wizard?workflow=internet")
        html = warning.get_data(as_text=True)
        self.assertIn("troubleshooting in progress", html)
        self.assertIn("Start new workflow", html)
        self.assertIn("Resume Session Test", html)


if __name__ == "__main__":
    unittest.main()
