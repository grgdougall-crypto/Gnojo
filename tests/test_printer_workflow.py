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


class PrinterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="printer-workflow-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_bounded_and_branch_aware(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(engine, "printer", catalog, catalog["printer"].get("version"))
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))

        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(
            report["metrics"]["reachable_nodes"],
            len(engine.workflow["nodes"]),
        )
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertGreater(report["metrics"]["terminal_nodes"], 0)
        self.assertGreaterEqual(
            report["metrics"]["longest_path"],
            report["metrics"]["shortest_path"],
        )
        self.assertLessEqual(
            report["metrics"]["longest_path"],
            report["metrics"]["reachable_nodes"],
        )
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))

    def test_power_uncertainty_gathers_evidence_then_terminates_safely(self):
        pages = self._run(["unknown", "", "unknown"])
        self.assertIn("Check the Printer Power", pages[1])
        self.assertIn("does the printer show any sign of power", pages[2])
        self.assertIn("Printer Power Requires Attention", pages[3])
        self.assertIn("Step 4 of 4 on this path", pages[3])

    def test_connection_uncertainty_becomes_a_bounded_connection_decision(self):
        pages = self._run(["yes", "unknown", "", "unknown"])
        self.assertIn("Identify the Printer Connection", pages[2])
        self.assertIn("Is a USB cable connected directly", pages[3])
        self.assertIn("Additional Troubleshooting Is Needed", pages[4])
        self.assertNotIn("How is the printer connected?", pages[4])

    def test_status_uncertainty_becomes_a_bounded_warning_decision(self):
        pages = self._run([
            "yes", "usb", "", "no", "other", "unknown", "", "unknown",
        ])
        self.assertIn("What best describes the printing problem?", pages[4])
        self.assertIn("Inspect the Printer Status", pages[6])
        self.assertIn("is a printer warning visible", pages[7])
        self.assertIn("Additional Troubleshooting Is Needed", pages[8])
        self.assertNotIn("Does the printer show a paper", pages[8])
        self._assert_complete(pages)

    def test_cleared_warning_is_verified_once_and_does_not_repeat_remediation(self):
        pages = self._run([
            "yes", "usb", "", "no", "other", "yes", "", "no",
        ])
        self.assertIn("Clear the Printer Warning", pages[6])
        self.assertIn("Can you print after clearing", pages[7])
        self.assertIn("Additional Troubleshooting Is Needed", pages[8])
        self.assertNotIn("Clear the Printer Warning", pages[8])
        self.assertNotIn("Does the printer show a paper", pages[8])
        self._assert_complete(pages)

    def test_long_path_never_reports_completion_before_interaction_finishes(self):
        pages = self._run([
            "unknown", "", "yes", "unknown", "", "yes", "", "no",
            "other", "unknown", "", "yes", "", "no",
        ])
        self.assertIn("Additional Troubleshooting Is Needed", pages[-1])
        self._assert_complete(pages)

    def test_short_success_path_ends_at_actual_length(self):
        pages = self._run(["yes", "usb", "", "yes"])
        self.assertIn("Printer Operation Restored", pages[-1])
        self._assert_complete(pages)

    def test_previous_restores_exact_node_and_progress(self):
        self._run([
            "unknown", "", "yes", "unknown", "", "yes", "", "no",
            "other", "unknown", "", "yes", "",
        ])
        verify = self.client.get(
            "/wizard?workflow=printer&resume=1", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Can you print after clearing", verify)
        verify_progress = self._progress(verify)

        previous = self._previous()
        self.assertIn("Clear the Printer Warning", previous)
        previous_progress = self._progress(previous)
        self.assertEqual(previous_progress[0], verify_progress[0] - 1)
        self.assertEqual(previous_progress[1], verify_progress[1])
        previous_again = self._previous()
        self.assertIn("is a printer warning visible", previous_again)
        previous_again_progress = self._progress(previous_again)
        self.assertEqual(previous_again_progress[0], previous_progress[0] - 1)
        self.assertEqual(previous_again_progress[1], previous_progress[1])

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=printer&restart=1"
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

    def _assert_complete(self, pages):
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)
        current, total = self._progress(pages[-1])
        self.assertEqual(current, total, pages[-1])


if __name__ == "__main__":
    unittest.main()
