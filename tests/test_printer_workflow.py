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
        self.assertEqual(report["metrics"]["reachable_nodes"], 18)
        self.assertEqual(report["metrics"]["terminal_nodes"], 3)
        self.assertEqual(report["metrics"]["shortest_path"], 4)
        self.assertEqual(report["metrics"]["longest_path"], 14)
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
        pages = self._run(["yes", "usb", "", "no", "unknown", "", "unknown"])
        self.assertIn("Inspect the Printer Status", pages[5])
        self.assertIn("is a printer warning visible", pages[6])
        self.assertIn("Additional Troubleshooting Is Needed", pages[7])
        self.assertNotIn("Does the printer show a paper", pages[7])

    def test_cleared_warning_is_verified_once_and_does_not_repeat_remediation(self):
        pages = self._run(["yes", "usb", "", "no", "yes", "", "no"])
        self.assertIn("Clear the Printer Warning", pages[5])
        self.assertIn("Can you print after clearing", pages[6])
        self.assertIn("Additional Troubleshooting Is Needed", pages[7])
        self.assertNotIn("Clear the Printer Warning", pages[7])
        self.assertNotIn("Does the printer show a paper", pages[7])

    def test_long_path_never_reports_completion_before_interaction_finishes(self):
        pages = self._run([
            "unknown", "", "yes", "unknown", "", "yes", "", "no",
            "unknown", "", "yes", "", "no",
        ])
        self.assertIn("Step 14 of 14 on this path", pages[-1])
        self.assertIn("Additional Troubleshooting Is Needed", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_short_success_path_ends_at_actual_length(self):
        pages = self._run(["yes", "usb", "", "yes"])
        self.assertIn("Printer Operation Restored", pages[-1])
        self.assertIn("Step 5 of 5 on this path", pages[-1])

    def test_previous_restores_exact_node_and_progress(self):
        self._run([
            "unknown", "", "yes", "unknown", "", "yes", "", "no",
            "unknown", "", "yes", "",
        ])
        verify = self.client.get(
            "/wizard?workflow=printer&resume=1", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Can you print after clearing", verify)
        self.assertIn("Step 13 of 14 on this path", verify)

        previous = self._previous()
        self.assertIn("Clear the Printer Warning", previous)
        self.assertIn("Step 12 of 14 on this path", previous)
        previous_again = self._previous()
        self.assertIn("is a printer warning visible", previous_again)
        self.assertIn("Step 11 of 14 on this path", previous_again)

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


if __name__ == "__main__":
    unittest.main()
