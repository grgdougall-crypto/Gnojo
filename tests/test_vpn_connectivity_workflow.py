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


class VPNConnectivityWorkflowTests(unittest.TestCase):
    LONG_PATH = [
        "no_internet_at_all", "no", "", "no", "", "no", "no", "", "yes",
        "no", "", "yes", "no", "", "no", "yes", "yes", "", "no", "",
        "no", "", "no", "yes", "", "no", "", "no", "no", "", "no", "",
        "no",
    ]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="vpn-connectivity-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_active_graph_is_clean_branch_aware_and_handoffs_exist(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine, "vpn_connectivity_win", catalog,
            catalog["vpn_connectivity_win"].get("version"),
        )
        workflow = engine.workflow
        report = WorkflowQualityValidator().validate(workflow, set(catalog))
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 39)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 5)
        self.assertEqual(report["metrics"]["terminating_paths"], 739)
        self.assertEqual(report["metrics"]["shortest_path"], 5)
        self.assertEqual(report["metrics"]["longest_path"], 34)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(workflow))
        expected = {
            "transition_general_network_troubleshoot": "internet",
            "transition_vpn_client_software_issue": "application_crash",
            "transition_vpn_error_code_escalation": "higher_layer_connectivity",
            "transition_advanced_vpn_troubleshoot": "higher_layer_connectivity",
        }
        for node_id, target in expected.items():
            with self.subTest(node_id=node_id):
                self.assertEqual(workflow["nodes"][node_id]["next_workflow"], target)
                self.assertIn(target, catalog)

    def test_short_client_launch_failure_uses_actual_total_and_valid_handoff(self):
        pages = self._run(["internet_works_no_vpn", "no", "", "no"])
        self.assertIn("VPN Client Software Failure", pages[-1])
        self.assertIn("Step 5 of 5 on this path", pages[-1])
        self.assertIn("Continue to Application Troubleshooting", pages[-1])
        application = self._post("")
        self.assertIn("Application Keeps Crashing", application)
        self.assertIn("Protect Your Work First", application)

    def test_general_network_and_error_handoffs_use_available_workflows(self):
        general = self._run(["no_internet_at_all", "no", "", "yes", "yes"])
        self.assertIn("General Network Troubleshooting Required", general[-1])
        self.assertIn("Continue to Internet Connection", general[-1])
        internet = self._post("")
        self.assertIn("Internet Connection", internet)
        self.assertIn("Can any other devices connect", internet)

        error = self._run(["internet_works_no_vpn", "yes", "yes", "yes", "no"])
        self.assertIn("Specific VPN Error Code Escalation", error[-1])
        higher = self._post("")
        self.assertIn("Higher-Layer Connectivity Diagnostics", higher)
        self.assertIn("What is still unable to connect?", higher)

    def test_deep_path_preserves_every_verification_and_has_no_premature_completion(self):
        pages = self._run(self.LONG_PATH)
        combined = "\n".join(pages)
        for expected in (
            "Did restarting the VPN client application resolve",
            "After rebooting your computer",
            "After checking your security software settings",
            "Are all relevant network adapters",
            "After enabling the adapter(s)",
            "After refreshing DNS and restarting Windows",
            "After reinstalling the VPN client",
        ):
            self.assertIn(expected, combined)
        self.assertIn("Advanced VPN Troubleshooting Required", pages[-1])
        self.assertIn("Step 34 of 34 on this path", pages[-1])
        self.assertIn("Continue to Higher-Layer Diagnostics", pages[-1])
        for page in pages[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total, page)

    def test_network_refresh_and_reinstall_wording_are_bounded_and_safe(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine, "vpn_connectivity_win", catalog,
            catalog["vpn_connectivity_win"].get("version"),
        )
        reset = engine.workflow["nodes"]["instr_reset_ip_stack"]["instruction"]
        self.assertIn("ipconfig /flushdns", reset)
        self.assertIn("managed device", reset)
        self.assertIn("Do not run broad `netsh` resets", reset)
        self.assertNotIn("ipconfig /release", reset)
        self.assertNotIn("ipconfig /renew", reset)
        reinstall = engine.workflow["nodes"]["instr_reinstall_vpn_client"]["instruction"]
        self.assertIn("approved software portal", reinstall)
        self.assertIn("official website", reinstall)
        self.assertIn("Do not download VPN installers from third-party", reinstall)

    def test_deep_handoff_and_previous_restore_exact_workflow_progress(self):
        pages = self._run(self.LONG_PATH)
        self.assertIn("Step 34 of 34 on this path", pages[-1])
        higher = self._post("")
        self.assertIn("Higher-Layer Connectivity Diagnostics", higher)
        self.assertIn("What is still unable to connect?", higher)

        restored_handoff = self._previous()
        self.assertIn("Advanced VPN Troubleshooting Required", restored_handoff)
        self.assertIn("Step 34 of 34 on this path", restored_handoff)
        restored_reinstall_result = self._previous()
        self.assertIn("After reinstalling the VPN client", restored_reinstall_result)
        self.assertIn("Step 33 of 34 on this path", restored_reinstall_result)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "vpn_connectivity_win")
            self.assertEqual(session["current_node"], "q_reinstall_client_works")
            self.assertEqual(session["step"], 33)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=vpn_connectivity_win&restart=1"
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
