import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_validation_service import WorkflowValidationService
import json


class AdvancedNetworkContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="continuation-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def _show_network_result(self, node_id="advanced_complete"):
        version = available_workflows()["network_diagnostics"].get("version")
        with self.client.session_transaction() as session:
            session["workflow"] = "network_diagnostics"
            session["workflow_version"] = version
            session["current_node"] = node_id
            session["step"] = 5
            session["node_history"] = [
                {
                    "workflow": "network_diagnostics",
                    "node_id": "dns_result",
                    "version": version,
                }
            ]
            session["workflow_complete"] = False
        return self.client.get(
            "/wizard?workflow=network_diagnostics&resume=1"
        )

    def test_core_network_result_exposes_guided_continuation_and_existing_actions(self):
        response = self._show_network_result()
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Core Network Diagnostics Passed", html)
        self.assertIn("Continue Troubleshooting", html)
        self.assertIn("Choose Another Workflow", html)
        self.assertIn("Restart This Workflow", html)
        self.assertIn('href="/"', html)
        self.assertIn(
            'href="/wizard?workflow=network_diagnostics&amp;restart=1"', html
        )

        choose_another = self.client.get("/")
        self.assertEqual(choose_another.status_code, 200)
        self.assertIn("Recommended Workflows", choose_another.get_data(as_text=True))

        restarted = self.client.get(
            "/wizard?workflow=network_diagnostics&restart=1",
            follow_redirects=True,
        )
        self.assertEqual(restarted.status_code, 200)
        self.assertIn("Inspect the IP Configuration", restarted.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertEqual(session["current_node"], "inspect_ip_configuration")
            self.assertNotIn("workflow_continuation", session)

    def test_continuation_enters_next_phase_without_generic_browsing(self):
        self._show_network_result()
        response = self.client.post("/wizard", follow_redirects=True)
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Higher-Layer Connectivity Diagnostics", html)
        self.assertIn("What is still unable to connect?", html)
        self.assertIn("Continuing from Advanced Network Diagnostics", html)
        self.assertNotIn("Choose a workflow", html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "higher_layer_connectivity")
            self.assertEqual(session["current_node"], "identify_remaining_scope")
            self.assertEqual(session["step"], 0)
            self.assertFalse(session["workflow_complete"])

    def test_previous_step_crosses_handoff_boundary_without_repeating_network_checks(self):
        self._show_network_result()
        self.client.post("/wizard")

        response = self.client.post(
            "/wizard",
            data={"navigation_action": "previous"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)

        self.assertIn("Core Network Diagnostics Passed", html)
        self.assertNotIn("Continuing from Advanced Network Diagnostics", html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "network_diagnostics")
            self.assertEqual(session["current_node"], "advanced_complete")
            self.assertNotIn("workflow_continuation", session)

    def test_other_terminal_results_do_not_gain_continuation(self):
        response = self._show_network_result("dns_problem")
        html = response.get_data(as_text=True)

        self.assertIn("DNS Resolution Problem", html)
        self.assertNotIn("Continue Troubleshooting", html)

    def test_existing_internet_to_advanced_diagnostics_handoff_is_unchanged(self):
        with self.client.session_transaction() as session:
            session["workflow"] = "internet"
            session["workflow_version"] = None
            session["current_node"] = "advanced_diagnostics"
            session["step"] = 5
            session["node_history"] = []
            session["workflow_complete"] = False

        response = self.client.post("/wizard", follow_redirects=True)
        html = response.get_data(as_text=True)

        self.assertIn("Advanced Network Diagnostics", html)
        self.assertIn("Inspect the IP Configuration", html)
        self.assertNotIn("Continuing from Advanced Network Diagnostics", html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "network_diagnostics")
            self.assertEqual(session["current_node"], "inspect_ip_configuration")

    def test_new_phase_is_valid_and_navigation_does_not_mutate_curator_or_workflow_data(self):
        project_root = Path(__file__).resolve().parents[1]
        protected_paths = [
            project_root / "curator",
            project_root / "app" / "decision_trees",
            project_root / "app" / "workflow_publications",
        ]

        def digest_tree():
            result = {}
            for directory in protected_paths:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        result[str(path.relative_to(project_root))] = hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
            return result

        workflow_path = (
            project_root / "app" / "decision_trees" / "higher_layer_connectivity.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        self.assertTrue(WorkflowValidationService().validate(workflow)["is_valid"])

        before = digest_tree()
        self._show_network_result()
        self.client.post("/wizard", follow_redirects=True)
        self.client.post("/wizard", data={"answer": "application"}, follow_redirects=True)
        after = digest_tree()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
