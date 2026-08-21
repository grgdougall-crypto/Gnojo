import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_validation_service import WorkflowValidationService


class DesktopSupportContentPackTests(unittest.TestCase):
    WORKFLOWS = {
        "application_crash": "Application Keeps Crashing",
        "no_sound": "No Sound",
        "low_storage": "Low Disk Space",
    }

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

    def test_workflows_are_valid_and_listed_as_windows_desktop_support(self):
        catalog = available_workflows()
        validator = WorkflowValidationService()
        for workflow_id, name in self.WORKFLOWS.items():
            workflow = json.loads(
                Path(f"app/decision_trees/{workflow_id}.json").read_text(encoding="utf-8")
            )
            result = validator.validate(workflow)
            self.assertEqual(result["errors"], [], workflow_id)
            self.assertEqual(result["warnings"], [], workflow_id)
            self.assertEqual(catalog[workflow_id]["name"], name)
            self.assertEqual(catalog[workflow_id]["category"], "Desktop Support")
            self.assertEqual(catalog[workflow_id]["platform"], "Windows")

    def test_each_workflow_opens_at_its_expected_first_step(self):
        expected = {
            "application_crash": "Protect Your Work First",
            "no_sound": "Where is sound missing?",
            "low_storage": "Protect Important Files",
        }
        for workflow_id, first_step in expected.items():
            with self.subTest(workflow_id=workflow_id):
                with self.client.session_transaction() as browser_session:
                    browser_session.clear()
                response = self.client.get(f"/wizard?workflow={workflow_id}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(first_step, response.get_data(as_text=True))

    def test_representative_paths_reach_clear_resolutions(self):
        paths = {
            "application_crash": (["", "one_file", "", "yes"], "File-Specific"),
            "no_sound": (["one_app", "", "yes"], "Application Audio Restored"),
            "low_storage": (["", "yes", "", "critical", "", "yes"], "Working Storage Restored"),
        }
        for workflow_id, (answers, expected) in paths.items():
            with self.subTest(workflow_id=workflow_id):
                self.client.get(f"/wizard?workflow={workflow_id}")
                response = None
                for answer in answers:
                    response = self.client.post(
                        "/wizard", data={"answer": answer}, follow_redirects=True
                    )
                self.assertIn(expected, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
