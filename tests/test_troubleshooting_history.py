import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class TroubleshootingHistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = TroubleshootingHistoryService(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_session_lifecycle_and_analytics(self):
        first = self.service.start(
            "internet", "Internet Connection", "check_scope",
            device={"id": "device", "name": "Work laptop", "platform": "Windows"},
            learning_mode=True,
        )
        self.service.progress(first["id"], "check_connection")
        self.service.progress(first["id"], "check_scope", action="back")
        completed = self.service.complete(first["id"], "resolved", "Connection restored")
        self.service.add_feedback(first["id"], {
            "solved": "yes",
            "clarity": 4,
            "confusing_step": "check_scope",
            "comment": "The first question could use an example.",
        })

        second = self.service.start("printer", "Printer", "start")
        self.service.abandon(second["id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["backtracks"], 1)
        self.assertEqual(completed["device"]["name"], "Work laptop")
        analytics = self.service.analytics()
        self.assertEqual(analytics["total"], 2)
        self.assertEqual(analytics["completed"], 1)
        self.assertEqual(analytics["abandoned"], 1)
        self.assertEqual(analytics["completion_rate"], 50)
        self.assertEqual(analytics["feedback_count"], 1)
        self.assertEqual(analytics["solved_rate"], 100)
        self.assertEqual(analytics["average_clarity"], 4.0)
        self.assertEqual(analytics["confusing_steps"][0]["node_id"], "check_scope")

    def test_feedback_requires_completed_session_and_valid_values(self):
        record = self.service.start("internet", "Internet", "start")
        with self.assertRaises(ValueError):
            self.service.add_feedback(record["id"], {"solved": "yes", "clarity": 5})
        self.service.complete(record["id"], "done")
        with self.assertRaises(ValueError):
            self.service.add_feedback(record["id"], {"solved": "maybe", "clarity": 8})

    def test_history_ids_are_validated_and_records_can_be_deleted(self):
        record = self.service.start("internet", "Internet", "start")
        self.service.delete(record["id"])
        self.assertIsNone(self.service.get(record["id"]))
        with self.assertRaises(ValueError):
            self.service.get("../private")


class TroubleshootingHistoryPageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = TroubleshootingHistoryService(Path(self.temporary.name))
        self.service_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.service
        )
        self.service_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.service_patch.stop()
        self.temporary.cleanup()

    def test_wizard_start_creates_private_local_history_and_page(self):
        response = self.client.get("/wizard?workflow=internet")
        self.assertEqual(response.status_code, 200)
        records = self.service.list()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "active")
        self.assertNotIn("answers", records[0])

        history = self.client.get("/troubleshooting-history")
        html = history.get_data(as_text=True)
        self.assertEqual(history.status_code, 200)
        self.assertIn("Troubleshooting History", html)
        self.assertIn("Internet Connection", html)
        self.assertIn("not the answers you enter", html)

        detail = self.client.get(
            f"/troubleshooting-history/{records[0]['id']}"
        )
        self.assertIn("Session path", detail.get_data(as_text=True))

    def test_restarting_marks_previous_session_abandoned(self):
        self.client.get("/wizard?workflow=internet")
        self.client.get("/wizard?workflow=internet&restart=1")
        records = self.service.list()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(item["status"] for item in records), ["abandoned", "active"]
        )

    def test_reaching_resolution_completes_history_record(self):
        self.client.get("/wizard?workflow=internet")
        for answer in ("yes", "wifi", "yes", "", "yes"):
            response = self.client.post("/wizard", data={"answer": answer}, follow_redirects=True)
        record = self.service.list()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["final_node_id"], "resolved")
        self.assertEqual(record["outcome"], "Internet connection restored")
        self.assertGreaterEqual(record["steps"], 6)
        self.assertIn("How did this workflow perform?", response.get_data(as_text=True))

        feedback = self.client.post(
            f"/api/troubleshooting-history/{record['id']}/feedback",
            json={
                "solved": "partially",
                "clarity": 3,
                "confusing_step": "verify_resolution",
                "comment": "A little more detail would help.",
            },
        )
        self.assertEqual(feedback.status_code, 200)
        saved = self.service.get(record["id"])["feedback"]
        self.assertEqual(saved["solved"], "partially")
        self.assertEqual(saved["clarity"], 3)

        history_html = self.client.get("/troubleshooting-history").get_data(as_text=True)
        self.assertIn("Workflow feedback", history_html)
        self.assertIn("Average clarity", history_html)
        detail_html = self.client.get(
            f"/troubleshooting-history/{record['id']}"
        ).get_data(as_text=True)
        self.assertIn("Workflow quality", detail_html)
        self.assertIn("A little more detail would help.", detail_html)


if __name__ == "__main__":
    unittest.main()
