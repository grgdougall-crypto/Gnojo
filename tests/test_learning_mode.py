import tempfile
import unittest
from unittest.mock import patch

from app.app import app
from app.models.node import Node
from app.services.learning_mode_service import LearningModeService
from app.services.workflow_publication_service import WorkflowPublicationService


class LearningModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.publications = WorkflowPublicationService(self.temp.name)
        self.workflow = {
            "workflow_id": "learning_test", "name": "VPN Learning Test", "category": "Networking",
            "platform": "Cross-platform", "estimated_steps": 2, "start_node": "question",
            "nodes": {
                "question": {"type": "question", "title": "VPN check", "question": "Can the VPN connect?", "help_text": "This separates tunnel failures from general access.", "answers": {"yes": {"label": "Yes", "next": "done"}}},
                "done": {"type": "resolution", "title": "Complete", "message": "VPN access is working."},
            },
        }
        self.publications.publish(self.workflow, "learning.json")
        self.client = app.test_client()
        self.patch = patch("app.app.WorkflowPublicationService", return_value=self.publications)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_learning_content_is_deterministic_and_uses_help_text(self):
        node = Node(id="vpn", type="question", question="Can the VPN connect?", help_text="This narrows authentication failures.")
        content = LearningModeService().build(node, "VPN Test")
        self.assertEqual(content["concepts"][0]["title"], "VPN fundamentals")
        self.assertEqual(content["what_it_checks"], "Can the VPN connect?")
        self.assertEqual(content["why_it_matters"], "This narrows authentication failures.")

    def test_learning_content_keeps_generic_fallback_when_specific_content_is_missing(self):
        node = Node(id="empty", type="question")
        content = LearningModeService().build(node, "Fallback Test")
        self.assertIn("narrows the problem space", content["what_it_checks"])
        self.assertIn("Clear observations", content["why_it_matters"])

    def test_learning_mode_is_optional_and_can_toggle_during_workflow(self):
        normal = self.client.get("/wizard?workflow=learning_test")
        self.assertNotIn("Understand This Step", normal.get_data(as_text=True))
        learning = self.client.get("/wizard?workflow=learning_test&resume=1&learning=1")
        html = learning.get_data(as_text=True)
        self.assertIn("Understand This Step", html)
        self.assertIn("What This Checks", html)
        self.assertIn("Why This Matters", html)
        self.assertIn("Can the VPN connect?", html)
        self.assertIn("This separates tunnel failures from general access.", html)
        self.assertNotIn("This question narrows the problem space", html)
        self.assertIn("VPN fundamentals", html)
        off = self.client.get("/wizard?workflow=learning_test&resume=1&learning=0")
        self.assertNotIn("Understand This Step", off.get_data(as_text=True))

    def test_learning_completion_lists_concepts_covered(self):
        self.client.get("/wizard?workflow=learning_test&learning=1")
        completed = self.client.post("/wizard", data={"answer": "yes"}, follow_redirects=True)
        html = completed.get_data(as_text=True)
        self.assertIn("Learning recap", html)
        self.assertIn("VPN fundamentals", html)
        self.assertIn("Concepts Covered in This Session", html)

    def test_landing_learning_card_enables_mode(self):
        response = self.client.get("/?learning=1")
        html = response.get_data(as_text=True)
        self.assertIn("Learning Mode is on", html)
        with self.client.session_transaction() as session:
            self.assertTrue(session["learning_mode"])


if __name__ == "__main__":
    unittest.main()
