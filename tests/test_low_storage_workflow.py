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


class LowStorageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="low-storage-workflow-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_terminating_and_preserves_safety_articles(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(engine, "low_storage", catalog, catalog["low_storage"].get("version"))
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))

        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 23)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 4)
        self.assertEqual(report["metrics"]["shortest_path"], 3)
        self.assertEqual(report["metrics"]["longest_path"], 19)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))
        nodes = engine.workflow["nodes"]
        self.assertEqual(nodes["protect_data"]["knowledge_article"], "windows-storage-performance")
        self.assertEqual(nodes["check_storage"]["knowledge_article"], "windows-storage-performance")
        self.assertEqual(nodes["cleanup_recommendations"]["knowledge_article"], "windows-storage-performance")
        self.assertIn("Do not manually delete", nodes["protect_data"]["instruction"])
        self.assertIn("unfamiliar folders", nodes["protect_data"]["instruction"])

    def test_initial_space_uncertainty_is_gathered_once_then_bounded(self):
        pages = self._run(["", "yes", "", "unsure", "", "unsure"])
        self.assertIn("Confirm Available Space", pages[4])
        self.assertIn("After checking the displayed amount", pages[5])
        self.assertIn("Storage Review Is Recommended", pages[6])
        self.assertNotIn("How much free space remains", pages[6])
        self.assertIn("Step 7 of 7 on this path", pages[6])

    def test_cleanup_recheck_uncertainty_does_not_return_to_cleanup_result(self):
        pages = self._run(["", "yes", "", "low", "", "unsure", "", "unsure"])
        self.assertIn("Recheck Available Space", pages[6])
        self.assertIn("After rechecking the displayed amount", pages[7])
        self.assertIn("Storage Review Is Recommended", pages[8])
        self.assertNotIn("Is there now enough free space", pages[8])

    def test_recycle_bin_has_verification_before_additional_cleanup(self):
        pages = self._run(["", "yes", "", "critical", "", "no"])
        self.assertIn("Review the Recycle Bin", pages[4])
        self.assertIn("Did reviewing and emptying the Recycle Bin", pages[5])
        self.assertIn("Use Cleanup Recommendations", pages[6])

    def test_short_safety_and_success_paths_use_actual_totals(self):
        safety = self._run(["", "no"])
        self.assertIn("Protect Important Data Before Cleanup", safety[-1])
        self.assertIn("Step 3 of 3 on this path", safety[-1])

        success = self._run(["", "yes", "", "low", "", "yes"])
        self.assertIn("Working Storage Restored", success[-1])
        self.assertIn("Step 7 of 7 on this path", success[-1])

    def test_long_path_has_no_premature_progress_and_terminates(self):
        pages = self._long_path(stop_before_final_answer=False)
        self.assertIn("Storage Review Is Recommended", pages[-1])
        self.assertIn("Step 19 of 19 on this path", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_previous_restores_exact_long_path_progress(self):
        pages = self._long_path(stop_before_final_answer=True)
        self.assertIn("Has adequate working space been restored?", pages[-1])
        self.assertIn("Step 18 of 19 on this path", pages[-1])

        previous = self._previous()
        self.assertIn("Uninstall the Approved Application", previous)
        self.assertIn("Step 17 of 19 on this path", previous)
        previous_again = self._previous()
        self.assertIn("Did you find a large application", previous_again)
        self.assertIn("Step 16 of 19 on this path", previous_again)

    def _long_path(self, stop_before_final_answer):
        answers = [
            "", "yes", "", "unsure", "", "critical", "", "unsure", "",
            "unsure", "", "unsure", "", "no", "", "yes", "",
        ]
        if not stop_before_final_answer:
            answers.append("no")
        return self._run(answers)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=low_storage&restart=1"
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
