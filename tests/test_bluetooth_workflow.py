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


class BluetoothWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="bluetooth-workflow-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_listed_device_uses_remove_and_repair_with_verification(self):
        pages = self._run(["yes", "", "yes", "yes", "", "yes"])
        self.assertIn("currently listed or saved", pages[3])
        self.assertIn("Remove and Re-pair the Device", pages[4])
        self.assertIn("Did the device connect after the pairing attempt?", pages[5])
        self.assertIn("Bluetooth Pairing Restored", pages[6])
        self.assertIn("Step 7 of 7 on this path", pages[6])

    def test_unlisted_device_uses_add_device_with_verification(self):
        pages = self._run(["yes", "", "yes", "no", "", "yes"])
        self.assertIn("Add the Bluetooth Device", pages[4])
        self.assertNotIn("Remove and Re-pair", pages[4])
        self.assertIn("Did the device connect after the pairing attempt?", pages[5])
        self.assertIn("Bluetooth Pairing Restored", pages[6])

    def test_repeated_uncertainty_is_bounded_after_evidence_check(self):
        pages = self._run(["yes", "", "yes", "unsure", "", "unsure"])
        self.assertIn("Check the Saved Bluetooth Device List", pages[4])
        self.assertIn("After checking the saved device list", pages[5])
        self.assertIn("Bluetooth Device Status Needs Review", pages[6])
        self.assertIn("Step 7 of 7 on this path", pages[6])
        self.assertNotIn("Continue", pages[6])

    def test_troubleshooter_and_service_actions_have_verification_boundaries(self):
        pages = self._run(
            ["yes", "", "yes", "yes", "", "no", "", "no", "", "yes", ""]
        )
        self.assertIn("Run the Bluetooth Troubleshooter", pages[6])
        self.assertIn("Did the troubleshooter restore", pages[7])
        self.assertIn("Restart the Bluetooth Support Service", pages[10])
        self.assertIn(
            "after the adapter and Bluetooth service or driver checks", pages[11]
        )
        self.assertNotIn("after the driver or service check", pages[11])

    def test_unresolved_route_ends_at_real_support_terminal_not_broken_handoff(self):
        pages = self._run(
            [
                "yes", "", "no", "", "no", "unsure", "", "yes", "", "no",
                "", "no", "", "yes", "", "no",
            ]
        )
        terminal = pages[-1]
        self.assertIn("Advanced Bluetooth Support Recommended", terminal)
        self.assertIn("Step 17 of 17 on this path", terminal)
        self.assertNotIn("Continue to Advanced Diagnostics", terminal)
        self.assertNotIn("name=\"next_workflow\"", terminal)

    def test_shortest_terminal_and_longest_route_progress_are_branch_aware(self):
        shortest = self._run(["unsure"])
        self.assertIn("Prepare the Bluetooth Device", shortest[-1])
        self.assertIn("Step 2 of 2 on this path", shortest[-1])

        longest = self._run(
            [
                "yes", "", "no", "", "no", "unsure", "", "yes", "", "no",
                "", "no", "", "yes", "", "no",
            ]
        )
        for page in longest[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total)
        self.assertEqual(self._progress(longest[-1]), (17, 17))

    def test_previous_restores_exact_node_and_progress(self):
        self._run(["yes", "", "yes", "yes", "", "no", "", "no", "", "yes"])
        question = self._post("")
        self.assertIn("Step 12 of 13 on this path", question)

        previous = self._previous()
        self.assertIn("Restart the Bluetooth Support Service", previous)
        self.assertIn("Step 11 of 13 on this path", previous)
        previous_again = self._previous()
        self.assertIn("Is the Bluetooth adapter present", previous_again)
        self.assertIn("Step 10 of 13 on this path", previous_again)

    def test_source_apostrophe_survives_runtime_rendering(self):
        page = self._run([])[0]
        self.assertRegex(page, r"device(?:'|&#39;|&#x27;)s power")
        self.assertNotIn("device s power", page)

    def test_validator_is_clean_and_graph_metrics_are_stable(self):
        publications = WorkflowPublicationService()
        current = publications.load_current("bt_win_not_connecting")
        workflow = current["workflow"]
        catalog = {item["workflow"]["workflow_id"] for item in publications.list_current()}
        report = WorkflowQualityValidator().validate(workflow, catalog)
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(
            report["metrics"],
            {
                "reachable_nodes": 25,
                "unreachable_nodes": 0,
                "terminal_nodes": 7,
                "terminating_paths": 53,
                "shortest_path": 2,
                "longest_path": 17,
                "cycles_detected": 0,
                "findings_count": 0,
                "errors": 0,
                "warnings": 0,
                "info": 0,
            },
        )
        self.assertTrue(WorkflowProgressService.enabled(workflow))

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=bt_win_not_connecting&restart=1"
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
