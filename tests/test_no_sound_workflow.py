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


class NoSoundWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="no-sound-workflow-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_bounded_and_preserves_articles(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(engine, "no_sound", catalog, catalog["no_sound"].get("version"))
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))

        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 20)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 7)
        self.assertEqual(report["metrics"]["shortest_path"], 4)
        self.assertEqual(report["metrics"]["longest_path"], 14)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))
        nodes = engine.workflow["nodes"]
        self.assertEqual(nodes["check_app_volume"]["knowledge_article"], "no-sound-check-app-volume")
        self.assertEqual(nodes["select_output"]["knowledge_article"], "no-sound-select-output")
        self.assertEqual(nodes["check_connection"]["knowledge_article"], "no-sound-check-connection")

    def test_scope_uncertainty_is_gathered_once_then_terminates_safely(self):
        pages = self._run(["unsure", "", "unsure"])
        self.assertIn("Test Sound in Another Application", pages[1])
        self.assertIn("After testing sound in another application", pages[2])
        self.assertIn("Audio Scope Could Not Be Confirmed", pages[3])
        self.assertNotIn("Where is sound missing?", pages[3])
        self.assertIn("Step 4 of 4 on this path", pages[3])

    def test_short_application_remediation_success_ends_at_actual_length(self):
        pages = self._run(["one_app", "", "yes"])
        self.assertIn("Application Audio Restored", pages[-1])
        self.assertIn("Step 4 of 4 on this path", pages[-1])

    def test_output_remediation_success_retains_existing_outcome(self):
        pages = self._run(["all_apps", "", "no", "", "yes"])
        self.assertIn("Correct Audio Output Selected", pages[-1])
        self.assertIn("Step 6 of 6 on this path", pages[-1])

    def test_long_remediation_failure_terminates_without_premature_progress(self):
        pages = self._run([
            "unsure", "", "one_app", "", "no", "", "no", "", "no", "",
            "no", "", "no",
        ])
        self.assertIn("Audio Device Support Is Recommended", pages[-1])
        self.assertIn("Step 14 of 14 on this path", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_troubleshooter_success_retains_existing_resolution(self):
        pages = self._run([
            "all_apps", "", "no", "", "no", "", "no", "", "yes",
        ])
        self.assertIn("Windows Audio Repaired", pages[-1])
        self.assertIn("Step 10 of 10 on this path", pages[-1])

    def test_previous_restores_exact_long_path_node_and_progress(self):
        self._run([
            "unsure", "", "one_app", "", "no", "", "no", "", "no", "",
            "no", "",
        ])
        result = self.client.get(
            "/wizard?workflow=no_sound&resume=1", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Did the Windows troubleshooter restore sound?", result)
        self.assertIn("Step 13 of 14 on this path", result)

        previous = self._previous()
        self.assertIn("Run the Windows Audio Troubleshooter", previous)
        self.assertIn("Step 12 of 14 on this path", previous)
        previous_again = self._previous()
        self.assertIn("Is sound working after reconnecting", previous_again)
        self.assertIn("Step 11 of 14 on this path", previous_again)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=no_sound&restart=1"
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
