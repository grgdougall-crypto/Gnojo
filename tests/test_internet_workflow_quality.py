import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows, load_runtime_workflow
from app.engine.decision_engine import DecisionEngine
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_progress_service import WorkflowProgressService
from app.services.workflow_quality_validator import WorkflowQualityValidator


class InternetWorkflowQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="internet-quality-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_bounded_and_branch_aware(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(engine, "internet", catalog, catalog["internet"].get("version"))
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))

        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 17)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 3)
        self.assertEqual(report["metrics"]["shortest_path"], 4)
        self.assertEqual(report["metrics"]["longest_path"], 12)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))

    def test_connection_uncertainty_is_gathered_once_then_handed_off(self):
        pages = self._run(["yes", "unknown", "", "unknown"])
        self.assertIn("Identify the Connection", pages[2])
        self.assertIn("After checking the network icon and cables", pages[3])
        self.assertIn("Continue to Advanced Network Diagnostics", pages[4])
        self.assertNotIn("How does this computer connect to the internet?", pages[4])
        self.assertIn("Step 5 of 5 on this path", pages[4])

    def test_wifi_uncertainty_is_gathered_once_then_handed_off(self):
        pages = self._run(["yes", "wifi", "unknown", "", "unknown"])
        self.assertIn("Check Wi-Fi Status", pages[3])
        self.assertIn("After checking the network controls", pages[4])
        self.assertIn("Continue to Advanced Network Diagnostics", pages[5])
        self.assertNotIn("Is Wi-Fi turned on?", pages[5])
        self.assertIn("Step 6 of 6 on this path", pages[5])

    def test_long_path_never_reports_completion_before_handoff(self):
        pages = self._run([
            "unknown", "", "yes", "unknown", "", "wifi", "unknown", "",
            "yes", "", "no",
        ])
        self.assertIn("Continue to Advanced Network Diagnostics", pages[-1])
        self.assertIn("Step 12 of 12 on this path", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_short_network_restart_success_ends_at_actual_length(self):
        pages = self._run(["no", "", "yes"])
        self.assertIn("Internet Connection Restored", pages[-1])
        self.assertIn("Step 4 of 4 on this path", pages[-1])

    def test_previous_restores_exact_long_path_node_and_progress(self):
        self._run([
            "unknown", "", "yes", "unknown", "", "wifi", "unknown", "",
            "yes", "",
        ])
        verify = self.client.get(
            "/wizard?workflow=internet&resume=1", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Can this computer connect to the internet now?", verify)
        self.assertIn("Step 11 of 12 on this path", verify)

        previous = self._previous()
        self.assertIn("Reconnect to Wi-Fi", previous)
        self.assertIn("Step 10 of 12 on this path", previous)
        previous_again = self._previous()
        self.assertIn("After checking the network controls", previous_again)
        self.assertIn("Step 9 of 12 on this path", previous_again)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=internet&restart=1"
            ).get_data(as_text=True)
        ]
        for answer in answers:
            pages.append(
                self.client.post(
                    "/wizard", data={"answer": answer}, follow_redirects=True
                ).get_data(as_text=True)
            )
        return pages

    def _previous(self):
        return self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)

    def _progress(self, page):
        match = re.search(r"Step (\d+) of (\d+) on this path", page)
        self.assertIsNotNone(match, page)
        return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    unittest.main()
