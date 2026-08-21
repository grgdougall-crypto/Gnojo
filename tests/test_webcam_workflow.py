import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_progress_service import WorkflowProgressService
from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_quality_validator import WorkflowQualityValidator


class WebcamWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="webcam-workflow-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_driver_action_requires_windows_camera_retest_before_resolution(self):
        pages = self._run(["yes", "", "no", "", "yes", ""])
        self.assertIn("Update or Reinstall Webcam Driver", pages[5])
        self.assertIn("does the webcam now work in Windows Camera", pages[6])
        self.assertNotIn("Driver Action Performed", pages[6])
        self.assertIn("Step 7 of 8 on this path", pages[6])

    def test_driver_retest_success_is_a_genuine_resolution(self):
        pages = self._run(["yes", "", "no", "", "yes", "", "yes"])
        self.assertIn("Webcam Restored After Driver Repair", pages[-1])
        self.assertIn("Step 8 of 8 on this path", pages[-1])
        self.assertNotIn("Continue", pages[-1])

    def test_driver_retest_failure_and_uncertainty_end_at_support(self):
        for answer in ("no", "unsure"):
            with self.subTest(answer=answer):
                pages = self._run(["yes", "", "no", "", "yes", "", answer])
                self.assertIn("Advanced Webcam Support Recommended", pages[-1])
                self.assertIn("Step 8 of 8 on this path", pages[-1])
                self.assertNotIn("Continue to", pages[-1])

    def test_application_specific_action_is_also_verified(self):
        pages = self._run(["yes", "", "yes", "", "yes"])
        self.assertIn("Verify Application Camera Settings", pages[3])
        self.assertIn("does the camera now work in that application", pages[4])
        self.assertIn("App Settings Fixed the Problem", pages[5])
        self.assertIn("Step 6 of 6 on this path", pages[5])

    def test_application_specific_failure_stays_bounded_and_scoped(self):
        pages = self._run(["yes", "", "yes", "", "no"])
        self.assertIn("Application Camera Support Recommended", pages[-1])
        self.assertIn("Windows Camera works", pages[-1])
        self.assertNotIn("Continue", pages[-1])

    def test_readiness_remediation_has_explicit_retest(self):
        pages = self._run(["no", "", "yes"])
        self.assertIn("Connect and Enable the Webcam", pages[1])
        self.assertIn("Does the webcam now work in Windows Camera", pages[2])
        self.assertIn("Webcam Connection Restored", pages[3])
        self.assertIn("Step 4 of 4 on this path", pages[3])

    def test_privacy_device_manager_and_driver_knowledge_links_are_preserved(self):
        workflow = self._workflow()
        self.assertEqual(
            workflow["nodes"]["i1_check_privacy"]["knowledge_article"],
            "webcam-not-working-windows-i1-check-privacy",
        )
        self.assertEqual(
            workflow["nodes"]["i3_device_manager"]["knowledge_article"],
            "using-device-manager-to-troubleshoot-hardware",
        )
        self.assertEqual(
            workflow["nodes"]["i4_update_driver"]["knowledge_article"],
            "webcam-not-working-windows-i4-update-driver",
        )

    def test_progress_recalculates_and_no_nonterminal_displays_completion(self):
        short = self._run(["no", "", "no"])
        self.assertEqual(self._progress(short[-1]), (4, 4))
        self.assertEqual(self._progress(short[1]), (2, 4))

        long = self._run(["yes", "", "no", "", "yes", "", "no"])
        for page in long[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total)
        self.assertEqual(self._progress(long[-1]), (8, 8))

        app_path = self._run(["yes", "", "yes"])
        self.assertEqual(self._progress(app_path[2]), (3, 8))
        self.assertEqual(self._progress(app_path[3]), (4, 6))

    def test_previous_restores_driver_action_and_exact_progress(self):
        self._run(["yes", "", "no", "", "yes", ""])
        previous = self._previous()
        self.assertIn("Update or Reinstall Webcam Driver", previous)
        self.assertIn("Step 6 of 8 on this path", previous)
        previous_again = self._previous()
        self.assertIn("Did you observe any error indicators", previous_again)
        self.assertIn("Step 5 of 8 on this path", previous_again)

    def test_validator_is_clean_all_routes_terminate_and_no_handoffs_remain(self):
        publications = WorkflowPublicationService()
        workflow = publications.load_current("webcam_not_working_windows")["workflow"]
        catalog = {item["workflow"]["workflow_id"] for item in publications.list_current()}
        report = WorkflowQualityValidator().validate(workflow, catalog)
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(
            report["metrics"],
            {
                "reachable_nodes": 17,
                "unreachable_nodes": 0,
                "terminal_nodes": 6,
                "terminating_paths": 11,
                "shortest_path": 4,
                "longest_path": 8,
                "cycles_detected": 0,
                "findings_count": 0,
                "errors": 0,
                "warnings": 0,
                "info": 0,
            },
        )
        self.assertTrue(WorkflowProgressService.enabled(workflow))
        self.assertFalse(
            any(node.get("type") == "transition" for node in workflow["nodes"].values())
        )

    def _workflow(self):
        return WorkflowPublicationService().load_current("webcam_not_working_windows")[
            "workflow"
        ]

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=webcam_not_working_windows&restart=1"
            ).get_data(as_text=True)
        ]
        pages.extend(self._post(answer) for answer in answers)
        return pages

    def _post(self, answer):
        return self.client.post(
            "/wizard", data={"answer": answer}, follow_redirects=True
        ).get_data(as_text=True)

    def _previous(self):
        return self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)

    @staticmethod
    def _progress(page):
        match = re.search(r"Step (\d+) of (\d+) on this path", page)
        if not match:
            raise AssertionError("Page did not contain branch-aware progress metadata")
        return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    unittest.main()
