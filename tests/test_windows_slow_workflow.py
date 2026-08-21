import json
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
from app.services.workflow_validation_service import WorkflowValidationService


class WindowsSlowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = json.loads(
            Path("app/decision_trees/windows_slow.json").read_text(encoding="utf-8")
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_workflow_is_valid_and_available_as_windows_desktop_support(self):
        result = WorkflowValidationService().validate(self.workflow)
        self.assertEqual(result["errors"], [])
        catalog = available_workflows()
        self.assertEqual(catalog["windows_slow"]["platform"], "Windows")
        self.assertEqual(catalog["windows_slow"]["category"], "Desktop Support")

    def test_active_graph_is_clean_branch_aware_and_storage_cleanup_is_not_duplicated(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine, "windows_slow", catalog, catalog["windows_slow"].get("version")
        )
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 33)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 10)
        self.assertEqual(report["metrics"]["shortest_path"], 2)
        self.assertEqual(report["metrics"]["longest_path"], 22)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))
        storage = engine.workflow["nodes"]["safe_storage_cleanup"]
        self.assertEqual(storage["type"], "transition")
        self.assertEqual(storage["next_workflow"], "low_storage")
        self.assertEqual(storage["knowledge_article"], "windows-storage-performance")
        self.assertNotIn("cleanup_improved", engine.workflow["nodes"])
        self.assertNotIn("resolved_storage", engine.workflow["nodes"])

    def test_application_path_reaches_resolution_and_records_history(self):
        start = self.client.get("/wizard?workflow=windows_slow")
        self.assertIn("Confirm the Windows Device", start.get_data(as_text=True))
        for answer in ("", "one_app", "", "yes"):
            response = self.client.post(
                "/wizard", data={"answer": answer}, follow_redirects=True
            )
        self.assertIn("Application Performance Restored", response.get_data(as_text=True))
        record = self.history.list()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["final_node_id"], "resolved_application")

    def test_task_manager_path_includes_learning_article(self):
        self.client.get("/wizard?workflow=windows_slow&learning=1")
        for answer in ("", "entire_system", "yes"):
            response = self.client.post(
                "/wizard", data={"answer": answer}, follow_redirects=True
            )
        html = response.get_data(as_text=True)
        self.assertIn("Inspect Resource Use in Task Manager", html)
        self.assertIn("Reading Windows Performance in Task Manager", html)
        self.assertIn("Processor utilization", html)

    def test_low_storage_handoff_and_cross_workflow_previous_restore_context(self):
        pages = self._run(["", "entire_system", "yes", "", "disk", "", "yes"])
        handoff = pages[-1]
        self.assertIn("Continue to Low Disk Space", handoff)
        self.assertNotIn("Continue to Advanced Diagnostics", handoff)
        self.assertIn("Low or uncertain free space may be contributing", handoff)
        self.assertIn("Step 8 of 8 on this path", handoff)
        self.assertNotIn("Free Space with Windows Recommendations", handoff)

        low_storage = self._post("")
        self.assertIn("Low Disk Space", low_storage)
        self.assertIn("Protect Important Files", low_storage)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "low_storage")
            self.assertEqual(session["current_node"], "protect_data")

        restored_handoff = self._previous()
        self.assertIn("Continue to Low Disk Space", restored_handoff)
        self.assertIn("Step 8 of 8 on this path", restored_handoff)
        restored_question = self._previous()
        self.assertIn("Is the Windows drive nearly full?", restored_question)
        self.assertIn("Step 7 of 18 on this path", restored_question)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "windows_slow")
            self.assertEqual(session["current_node"], "storage_result")
            self.assertEqual(session["step"], 7)

    def test_storage_uncertainty_is_bounded_by_the_dedicated_handoff(self):
        pages = self._run(["", "entire_system", "yes", "", "disk", "", "unsure"])
        self.assertIn("Continue to Low Disk Space", pages[-1])
        self.assertNotIn("Free Space with Windows Recommendations", pages[-1])

    def test_startup_change_has_a_restart_and_result_boundary(self):
        pages = self._run(["", "startup", "", "yes", ""])
        self.assertIn("Review Startup Applications", pages[2])
        self.assertIn("Test the Lighter Startup", pages[4])
        self.assertIn("Did reducing startup load improve performance?", pages[5])

    def test_security_scan_and_update_results_retain_both_outcomes(self):
        threat = self._run(["", "startup", "", "no", "", "threats"])
        self.assertIn("Security Follow-Up Required", threat[-1])

        clean = self._run(["", "startup", "", "no", "", "clean", ""])
        self.assertIn("Are Windows updates pending?", clean[-1])
        no_updates = self._post("no")
        self.assertIn("Deeper Performance Diagnostics Recommended", no_updates)

    def test_windows_update_installation_has_a_result_boundary(self):
        pages = self._run(["", "startup", "", "no", "", "clean", "", "yes", ""])
        self.assertIn("Install Approved Updates", pages[-2])
        self.assertIn("Is performance acceptable after updates", pages[-1])
        resolved = self._post("yes")
        self.assertIn("Updates Restored Performance", resolved)

    def test_long_path_never_reports_completion_before_terminal(self):
        pages = self._run([
            "", "entire_system", "no", "", "no", "", "cpu", "", "no", "",
            "no", "", "yes", "", "no", "", "clean", "", "yes", "", "no",
        ])
        self.assertIn("Deeper Performance Diagnostics Recommended", pages[-1])
        self.assertIn("Step 22 of 22 on this path", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_short_application_path_uses_actual_total_and_previous_restores_progress(self):
        pages = self._run(["", "one_app", "", "yes"])
        self.assertIn("Application Performance Restored", pages[-1])
        self.assertIn("Step 5 of 5 on this path", pages[-1])
        previous = self._previous()
        self.assertIn("Is the application responding normally", previous)
        self.assertIn("Step 4 of 5 on this path", previous)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=windows_slow&restart=1"
            ).get_data(as_text=True)
        ]
        for answer in answers:
            pages.append(self._post(answer))
        return pages

    def _post(self, answer):
        return self.client.post(
            "/wizard", data={"answer": answer}, follow_redirects=True
        ).get_data(as_text=True)

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
