import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_progress_service import WorkflowProgressService
from app.services.workflow_publication_service import WorkflowPublicationService


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

    def test_long_service_path_does_not_report_completion_before_terminal(self):
        pages = self._run(["yes", "", "yes", "", "no", "", "no", "", "yes"])
        service = pages[-1]
        self.assertIn("Restart the Bluetooth Support Service", service)
        self.assertIn("Step 10 of 12 on this path", service)
        self.assertNotIn("Step 10 of 10", service)
        self.assertIn("Continue", service)

        final_question = self._post("")
        self.assertIn("Does the Bluetooth device connect after", final_question)
        self.assertIn("Step 11 of 12 on this path", final_question)
        terminal = self._post("no")
        self.assertIn("Advanced Bluetooth Support Recommended", terminal)
        self.assertIn("Step 12 of 12 on this path", terminal)
        self.assertIn("Continue to Advanced Diagnostics", terminal)

    def test_short_resolution_ends_at_its_actual_journey_length(self):
        pages = self._run(["yes", "", "yes", "", "yes"])
        self.assertIn("Bluetooth Pairing Restored", pages[-1])
        self.assertIn("Step 6 of 6 on this path", pages[-1])
        self.assertNotIn("Continue", pages[-1])

    def test_uncertainty_is_bounded_at_a_safe_terminal(self):
        pages = self._run(["unsure"])
        self.assertIn("Prepare the Bluetooth Device", pages[-1])
        self.assertIn("Step 2 of 2 on this path", pages[-1])
        self.assertNotIn("Continue", pages[-1])

    def test_previous_restores_exact_node_and_progress(self):
        self._run(["yes", "", "yes", "", "no", "", "no", "", "yes"])
        question = self._post("")
        self.assertIn("Step 11 of 12 on this path", question)

        previous = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Restart the Bluetooth Support Service", previous)
        self.assertIn("Step 10 of 12 on this path", previous)
        previous_again = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Is the Bluetooth adapter present", previous_again)
        self.assertIn("Step 9 of 12 on this path", previous_again)

    def test_progress_graph_is_acyclic_and_terminals_have_no_local_continuation(self):
        current = WorkflowPublicationService().load_current("bt_win_not_connecting")
        workflow = current["workflow"]
        self.assertTrue(WorkflowProgressService.enabled(workflow))
        nodes = workflow["nodes"]
        self.assertEqual(
            WorkflowProgressService._longest_path(
                nodes, workflow["start_node"], frozenset()
            ),
            14,
        )
        self.assertFalse(self._has_cycle(nodes, workflow["start_node"], set(), set()))
        for node_id, node in nodes.items():
            if node.get("type") in {"resolution", "transition"}:
                with self.subTest(node_id=node_id):
                    self.assertNotIn("next", node)
            if node.get("type") == "resolution":
                with self.subTest(node_id=node_id):
                    self.assertNotIn("next_workflow", node)

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

    @classmethod
    def _has_cycle(cls, nodes, node_id, visiting, visited):
        if node_id in visiting:
            return True
        if node_id in visited or node_id not in nodes:
            return False
        visiting.add(node_id)
        for target in WorkflowProgressService._next_ids(nodes[node_id]):
            if cls._has_cycle(nodes, target, visiting, visited):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False


if __name__ == "__main__":
    unittest.main()
