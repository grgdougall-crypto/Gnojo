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


class ExternalMonitorWorkflowTests(unittest.TestCase):
    PATHS = (
        (["yes", "", "yes"], "Issue Resolved: Cable or Port Connection", 4),
        (["yes", "", "no", "", "yes"], "Issue Resolved: Windows Display Settings", 6),
        (["yes", "", "no", "", "no"], "Additional Display Troubleshooting Is Needed", 6),
        (["no", "", "yes"], "Issue Resolved: Basic Monitor Check", 4),
        (["no", "", "no", "", "yes"], "Issue Resolved: Cable or Port Connection", 6),
        (["no", "", "no", "", "no", "", "yes"], "Issue Resolved: Windows Display Settings", 8),
        (["no", "", "no", "", "no", "", "no"], "Additional Display Troubleshooting Is Needed", 8),
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="external-monitor-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_and_branch_aware(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine,
            "external_monitor_not_detected_windows",
            catalog,
            catalog["external_monitor_not_detected_windows"].get("version"),
        )
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 11)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 4)
        self.assertEqual(report["metrics"]["terminating_paths"], 7)
        self.assertEqual(report["metrics"]["shortest_path"], 4)
        self.assertEqual(report["metrics"]["longest_path"], 8)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))

    def test_every_terminal_path_uses_its_actual_length_without_early_completion(self):
        for answers, title, length in self.PATHS:
            with self.subTest(answers=answers, title=title):
                pages = self._run(answers)
                self.assertIn(title, pages[-1])
                self.assertIn(f"Step {length} of {length} on this path", pages[-1])
                for page in pages[:-1]:
                    current, total = self._progress(page)
                    self.assertLess(current, total, page)

    def test_previous_restores_exact_long_path_progress(self):
        pages = self._run(["no", "", "no", "", "no", "", "no"])
        self.assertIn("Step 8 of 8 on this path", pages[-1])

        previous = self._previous()
        self.assertIn("Is the monitor now displaying an image", previous)
        self.assertIn("Step 7 of 8 on this path", previous)
        previous_again = self._previous()
        self.assertIn("Force Windows to Detect Display", previous_again)
        self.assertIn("Step 6 of 8 on this path", previous_again)
        with self.client.session_transaction() as session:
            self.assertEqual(session["current_node"], "i_force_detect_display")
            self.assertEqual(session["step"], 6)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=external_monitor_not_detected_windows&restart=1"
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
