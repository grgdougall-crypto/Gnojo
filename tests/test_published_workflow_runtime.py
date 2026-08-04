import tempfile
import unittest
from unittest.mock import patch

from app.app import app
from app.services.workflow_publication_service import WorkflowPublicationService


class PublishedWorkflowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.publications = WorkflowPublicationService(self.temp.name)
        self.workflow = {
            "workflow_id": "live_test",
            "name": "Live Published Test",
            "description": "A published workflow available to troubleshooters.",
            "estimated_steps": 2,
            "start_node": "start",
            "nodes": {
                "start": {
                    "type": "question",
                    "title": "Starting question",
                    "question": "Did the published workflow load?",
                    "answers": {"yes": {"label": "Yes", "next": "done"}},
                },
                "done": {"type": "resolution", "title": "Complete", "message": "Published workflow completed."},
            },
        }
        self.publications.publish(self.workflow, "live_test.json", "Ready for users")
        self.client = app.test_client()
        self.patch = patch("app.app.WorkflowPublicationService", return_value=self.publications)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_published_workflow_appears_on_home_page(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Live Published Test", html)
        self.assertIn("Published · v1", html)
        self.assertIn("/wizard?workflow=live_test", html)

    def test_wizard_runs_the_published_snapshot(self):
        start = self.client.get("/wizard?workflow=live_test")
        self.assertEqual(start.status_code, 200)
        self.assertIn("Did the published workflow load?", start.get_data(as_text=True))

        resolution = self.client.post("/wizard", data={"answer": "yes"}, follow_redirects=True)
        self.assertEqual(resolution.status_code, 200)
        self.assertIn("Published workflow completed.", resolution.get_data(as_text=True))

    def test_built_in_workflows_remain_available(self):
        response = self.client.get("/wizard?workflow=internet")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Internet Connection", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
