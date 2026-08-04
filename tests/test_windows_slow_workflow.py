import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
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

    def test_application_path_reaches_resolution_and_records_history(self):
        start = self.client.get("/wizard?workflow=windows_slow")
        self.assertIn("Confirm the Windows device", start.get_data(as_text=True))
        for answer in ("", "one_app", "", "yes"):
            response = self.client.post(
                "/wizard", data={"answer": answer}, follow_redirects=True
            )
        self.assertIn("Application performance restored", response.get_data(as_text=True))
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
        self.assertIn("Inspect resource use in Task Manager", html)
        self.assertIn("Reading Windows Performance in Task Manager", html)
        self.assertIn("Processor utilization", html)


if __name__ == "__main__":
    unittest.main()
