import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.content_quality_service import ContentQualityService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class ContentQualityServiceTests(unittest.TestCase):
    def test_report_prioritizes_feedback_abandonment_and_coverage(self):
        workflows = {
            "slow": {
                "workflow_id": "slow", "name": "Slow Computer",
                "category": "Desktop Support", "platform": "Windows",
                "nodes": {
                    "start": {"type": "question", "question": "Slow?", "answers": {}},
                    "step": {"type": "instruction", "title": "Check", "instruction": "Check it."},
                    "done": {"type": "resolution", "title": "Done"},
                },
            }
        }
        records = [
            {"workflow_id": "slow", "status": "completed", "feedback": {"solved": "no", "clarity": 2, "confusing_step": "step"}},
            {"workflow_id": "slow", "status": "completed", "feedback": {"solved": "no", "clarity": 2, "confusing_step": "step"}},
            {"workflow_id": "slow", "status": "abandoned"},
        ]
        report = ContentQualityService().build(workflows, records, {"slow": "slow.json"})
        kinds = {item["kind"] for item in report["action_queue"]}
        self.assertTrue({"effectiveness", "clarity", "abandonment", "confusing_step", "knowledge", "learning"}.issubset(kinds))
        self.assertGreaterEqual(report["summary"]["high_priority"], 3)
        self.assertEqual(report["workflows"][0]["solved_rate"], 0)
        confusing = next(item for item in report["action_queue"] if item["kind"] == "confusing_step")
        self.assertEqual(confusing["node_id"], "step")
        self.assertEqual(confusing["filename"], "slow.json")


class ContentQualityPageTests(unittest.TestCase):
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

    def test_content_studio_links_to_quality_dashboard(self):
        html = self.client.get("/content-studio").get_data(as_text=True)
        self.assertIn("Content Quality Dashboard", html)
        self.assertIn('href="/content-quality"', html)

    def test_dashboard_renders_queue_health_and_coverage(self):
        response = self.client.get("/content-quality")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("What Needs Attention", html)
        self.assertIn("Library Overview", html)
        self.assertIn("Coverage Snapshot", html)
        self.assertIn("Computer Running Slowly", html)
        self.assertIn("Knowledge coverage is thin", html)

    def test_editor_supports_direct_node_selection(self):
        html = self.client.get(
            "/workflow-editor/vpn_connectivity_win.json?node=instr_check_adapter_status"
        ).get_data(as_text=True)
        self.assertIn('data-node-id="instr_check_adapter_status"', html)
        self.assertIn("requestedNodeId", html)


if __name__ == "__main__":
    unittest.main()
