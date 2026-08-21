import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class InternetUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="internet-uncertainty-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_repeated_scope_uncertainty_ends_safely_and_preserves_history_progress(self):
        start = self.client.get("/wizard?workflow=internet&restart=1")
        self.assertIn("Can any other devices connect to the internet?", start.get_data(as_text=True))
        self.assertIn("Step 1 of 12 on this path", start.get_data(as_text=True))

        evidence = self.client.post(
            "/wizard", data={"answer": "unknown"}, follow_redirects=True
        )
        self.assertIn("Test Another Device", evidence.get_data(as_text=True))
        self.assertIn("Step 2 of 12 on this path", evidence.get_data(as_text=True))

        second_question = self.client.post("/wizard", follow_redirects=True)
        self.assertIn(
            "Can any other devices connect to the internet?",
            second_question.get_data(as_text=True),
        )
        self.assertIn("Step 3 of 12 on this path", second_question.get_data(as_text=True))

        outcome = self.client.post(
            "/wizard", data={"answer": "unknown"}, follow_redirects=True
        )
        outcome_html = outcome.get_data(as_text=True)
        self.assertIn("Network Scope Could Not Be Confirmed", outcome_html)
        self.assertNotIn("Test Another Device", outcome_html)
        self.assertIn("Step 4 of 4 on this path", outcome_html)
        self.assertIn('aria-valuenow="100"', outcome_html)

        previous = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        )
        previous_html = previous.get_data(as_text=True)
        self.assertIn("Can any other devices connect to the internet?", previous_html)
        self.assertIn("Step 3 of 12 on this path", previous_html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["current_node"], "confirm_scope_after_test")
            self.assertEqual(session["step"], 3)

    def test_initial_scope_yes_and_no_branches_are_unchanged(self):
        yes = self._answer_initial("yes")
        self.assertIn("How does this computer connect to the internet?", yes)
        no = self._answer_initial("no")
        self.assertIn("Restart the Network Equipment", no)

    def test_post_test_scope_yes_and_no_branches_match_initial_destinations(self):
        yes = self._answer_after_test("yes")
        self.assertIn("How does this computer connect to the internet?", yes)
        no = self._answer_after_test("no")
        self.assertIn("Restart the Network Equipment", no)

    def _answer_initial(self, answer):
        self.client.get("/wizard?workflow=internet&restart=1")
        return self.client.post(
            "/wizard", data={"answer": answer}, follow_redirects=True
        ).get_data(as_text=True)

    def _answer_after_test(self, answer):
        version = available_workflows()["internet"].get("version")
        with self.client.session_transaction() as session:
            session["workflow"] = "internet"
            session["workflow_version"] = version
            session["current_node"] = "confirm_scope_after_test"
            session["step"] = 3
            session["node_history"] = []
            session["workflow_complete"] = False
        return self.client.post(
            "/wizard", data={"answer": answer}, follow_redirects=True
        ).get_data(as_text=True)


if __name__ == "__main__":
    unittest.main()
