import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows, load_runtime_workflow
from app.engine.decision_engine import DecisionEngine
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_quality_validator import WorkflowQualityValidator
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
            session["step"] = 7 if node_id == "advanced_complete" else 5
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
        self.assertIn("Continue to Higher-Layer Diagnostics", html)
        self.assertIn("Previous Step", html)

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

        self.assertIn("Continue to Higher-Layer Diagnostics", html)
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

    def test_previous_restores_internet_step_metadata_across_handoff(self):
        internet_version = available_workflows()["internet"].get("version")
        with self.client.session_transaction() as session:
            session["workflow"] = "internet"
            session["workflow_version"] = internet_version
            session["current_node"] = "verify_resolution"
            session["step"] = 5
            session["node_history"] = []
            session["workflow_complete"] = False

        handoff = self.client.post(
            "/wizard", data={"answer": "no"}, follow_redirects=True
        )
        self.assertIn("Continue to Advanced Network Diagnostics", handoff.get_data(as_text=True))
        self.assertIn("Step 6 of 6 on this path", handoff.get_data(as_text=True))
        self.assertIn('aria-valuenow="100"', handoff.get_data(as_text=True))

        advanced = self.client.post("/wizard", follow_redirects=True)
        self.assertIn("Advanced Network Diagnostics", advanced.get_data(as_text=True))
        self.assertIn("Step 1 of 7 on this path", advanced.get_data(as_text=True))

        restored_handoff = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        )
        handoff_html = restored_handoff.get_data(as_text=True)
        self.assertIn("Continue to Advanced Network Diagnostics", handoff_html)
        self.assertIn("Step 6 of 6 on this path", handoff_html)
        self.assertIn('aria-valuenow="100"', handoff_html)

        restored_question = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        )
        question_html = restored_question.get_data(as_text=True)
        self.assertIn("Can this computer connect to the internet now?", question_html)
        self.assertIn("Step 5 of 6 on this path", question_html)
        with self.client.session_transaction() as session:
            self.assertEqual(session["workflow"], "internet")
            self.assertEqual(session["current_node"], "verify_resolution")
            self.assertEqual(session["step"], 5)

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

    def test_short_medium_and_long_routes_use_actual_branch_totals(self):
        short = self._run_network(["", "apipa"])
        self.assertIn("Step 3 of 3 on this path", short[-1])
        self.assertIn('aria-valuenow="100"', short[-1])

        medium = self._run_network(["", "normal", "", "no"])
        self.assertIn("Step 5 of 5 on this path", medium[-1])
        self.assertIn('aria-valuenow="100"', medium[-1])

        long = self._run_network(["", "normal", "", "yes", "", "yes"])
        self.assertIn("Step 5 of 7 on this path", long[4])
        self.assertNotIn("Step 5 of 5", long[4])
        self.assertIn("Step 6 of 7 on this path", long[5])
        self.assertIn("Step 7 of 7 on this path", long[6])
        self.assertIn("Continue to Higher-Layer Diagnostics", long[6])
        for page in long[:-1]:
            current, total = self._progress(page)
            self.assertLess(current, total)
            expected = round((current / total) * 100)
            self.assertIn(f'aria-valuenow="{expected}"', page)

    def test_previous_restores_long_route_node_and_progress(self):
        self._run_network(["", "normal", "", "yes", ""])
        self.assertIn("Step 6 of 7 on this path", self.client.get(
            "/wizard?workflow=network_diagnostics&resume=1"
        ).get_data(as_text=True))

        previous = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Test DNS Resolution", previous)
        self.assertIn("Step 5 of 7 on this path", previous)
        self.assertIn('aria-valuenow="71"', previous)

        previous_again = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Did the default gateway respond?", previous_again)
        self.assertIn("Step 4 of 7 on this path", previous_again)

    def test_published_graph_is_clean_and_handoff_is_explicit(self):
        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine,
            "network_diagnostics",
            catalog,
            catalog["network_diagnostics"].get("version"),
        )
        report = WorkflowQualityValidator().validate(engine.workflow, set(catalog))
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["metrics"]["reachable_nodes"], 12)
        self.assertEqual(report["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(report["metrics"]["terminal_nodes"], 6)
        self.assertEqual(report["metrics"]["terminating_paths"], 6)
        self.assertEqual(report["metrics"]["shortest_path"], 3)
        self.assertEqual(report["metrics"]["longest_path"], 7)
        handoff = engine.workflow["nodes"]["advanced_complete"]
        self.assertEqual(handoff["type"], "transition")
        self.assertEqual(handoff["next_workflow"], "higher_layer_connectivity")
        self.assertEqual(
            handoff["button_label"], "Continue to Higher-Layer Diagnostics"
        )

    def _run_network(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=network_diagnostics&restart=1"
            ).get_data(as_text=True)
        ]
        for answer in answers:
            pages.append(
                self.client.post(
                    "/wizard", data={"answer": answer}, follow_redirects=True
                ).get_data(as_text=True)
            )
        return pages

    def _progress(self, page):
        import re

        match = re.search(r"Step (\d+) of (\d+) on this path", page)
        self.assertIsNotNone(match, page)
        return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    unittest.main()
