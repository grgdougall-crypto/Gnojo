import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_progress_service import WorkflowProgressService


class ApplicationCrashWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="application-crash-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_long_crash_path_never_reports_terminal_progress_early(self):
        pages = self._run(["", "crashes", "no", "", "no", "", "no", "", "yes", "", "no"])
        self.assertIn("Check for an Approved Application Update", pages[7])
        self.assertNotIn("Step 8 of 8", pages[7])
        self.assertIn("Was an approved update installed?", pages[8])
        self.assertIn("Test After the Update", pages[9])
        self.assertIn("Is the application stable now?", pages[10])
        self.assertIn("Application Support Is Recommended", pages[11])
        self.assertIn("Step 12 of 12 on this path", pages[11])
        self.assertIn('aria-valuenow="100"', pages[11])

    def test_wont_open_short_branch_has_coherent_branch_relative_progress(self):
        pages = self._run(["", "wont_open", "", "no", ""])
        self.assertIn("Restart Windows", pages[2])
        self.assertIn("Step 3 of 9 on this path", pages[2])
        self.assertIn("Was an approved update installed?", pages[5])
        terminal = self.client.post(
            "/wizard", data={"answer": "no"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Application Support Is Recommended", terminal)
        self.assertIn("Step 7 of 7 on this path", terminal)

    def test_file_specific_branch_is_bounded_safe_and_previous_restores_progress(self):
        pages = self._run(["", "one_file", "", "yes"])
        self.assertIn("Test a Different File Safely", pages[2])
        self.assertIn("Does the application work with the other file?", pages[3])
        self.assertIn("The Issue Appears File-Specific", pages[4])
        self.assertIn("Step 5 of 5 on this path", pages[4])
        previous = self.client.post(
            "/wizard", data={"navigation_action": "previous"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Does the application work with the other file?", previous)
        self.assertIn("Step 4 of 11 on this path", previous)
        with self.client.session_transaction() as session:
            self.assertEqual(session["current_node"], "other_file_result")
            self.assertEqual(session["step"], 4)

    def test_scope_yes_no_and_uncertainty_paths_remain_bounded(self):
        yes = self._run(["", "crashes", "yes"])[-1]
        self.assertIn("Use the Computer Performance Workflow", yes)
        no = self._run(["", "crashes", "no"])[-1]
        self.assertIn("Close and Reopen the Application", no)
        unsure = self._run(["", "crashes", "unsure"])[-1]
        self.assertIn("Close and Reopen the Application", unsure)

    def test_progress_graph_is_acyclic_and_all_terminals_are_terminal(self):
        catalog = available_workflows()
        self.client.get("/wizard?workflow=application_crash&restart=1")
        with self.client.session_transaction() as session:
            version = session["workflow_version"]
        from app.services.workflow_publication_service import WorkflowPublicationService
        workflow = WorkflowPublicationService().load_version(
            "application_crash", version
        )["workflow"]
        self.assertTrue(WorkflowProgressService.enabled(workflow))
        nodes = workflow["nodes"]
        for node_id, node in nodes.items():
            with self.subTest(node_id=node_id):
                if node.get("type") == "resolution":
                    self.assertNotIn("next", node)
                    self.assertNotIn("next_workflow", node)
        self.assertEqual(
            WorkflowProgressService._longest_path(nodes, workflow["start_node"], frozenset()),
            12,
        )
        self.assertFalse(self._has_cycle(nodes, workflow["start_node"], set(), set()))

    def _run(self, answers):
        pages = []
        response = self.client.get("/wizard?workflow=application_crash&restart=1")
        pages.append(response.get_data(as_text=True))
        for answer in answers:
            response = self.client.post(
                "/wizard", data={"answer": answer}, follow_redirects=True
            )
            pages.append(response.get_data(as_text=True))
        return pages

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
