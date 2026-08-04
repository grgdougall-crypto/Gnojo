import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class EndTroubleshootingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch("app.app.TroubleshootingHistoryService", return_value=self.history)
        self.history_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_home_offers_end_session_control(self):
        self.client.get("/wizard?workflow=internet")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("End session", html)
        self.assertIn('action="/troubleshooting-session/end"', html)
        self.assertIn("Resume troubleshooting", html)

    def test_ending_session_clears_progress_and_marks_history_abandoned(self):
        self.client.get("/wizard?workflow=internet")
        record_id = self.history.list()[0]["id"]
        response = self.client.post("/troubleshooting-session/end", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Saved troubleshooting progress cleared", html)
        self.assertNotIn("Resume troubleshooting", html)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("workflow", browser_session)
            self.assertNotIn("current_node", browser_session)
            self.assertNotIn("troubleshooting_history_id", browser_session)
        self.assertEqual(self.history.get(record_id)["status"], "abandoned")

    def test_endpoint_requires_post(self):
        self.assertEqual(self.client.get("/troubleshooting-session/end").status_code, 405)


if __name__ == "__main__":
    unittest.main()
