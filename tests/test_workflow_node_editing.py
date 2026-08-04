import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_draft_service import (
    WorkflowDraftError,
    WorkflowDraftService,
)


class WorkflowNodeEditingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.drafts_path = Path(self.temporary_directory.name)
        self.filename = "example.json"
        self.draft_path = self.drafts_path / self.filename
        self.workflow = {
            "workflow_id": "example",
            "name": "Example",
            "nodes": {
                "question_one": {
                    "type": "question",
                    "question": "Original question?",
                    "help_text": "Original help",
                    "answers": {
                        "yes": {"label": "Yes", "next": "done"},
                    },
                },
                "done": {
                    "type": "resolution",
                    "title": "Done",
                    "message": "Original message",
                },
            },
        }
        self.draft_path.write_text(
            json.dumps(self.workflow, indent=4),
            encoding="utf-8",
        )
        self.service = WorkflowDraftService(self.drafts_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_update_node_persists_allowed_fields_and_answers(self):
        self.service.update_node(
            self.filename,
            "question_one",
            {
                "question": "Updated question?",
                "help_text": "",
                "answers": {
                    "yes": {"label": "It works", "next": "done"},
                    "no": {"label": "Still broken", "next": "question_one"},
                },
            },
        )

        saved = json.loads(self.draft_path.read_text(encoding="utf-8"))
        node = saved["nodes"]["question_one"]
        self.assertEqual(node["question"], "Updated question?")
        self.assertNotIn("help_text", node)
        self.assertEqual(node["answers"]["no"]["next"], "question_one")
        self.assertEqual(saved["nodes"]["done"]["message"], "Original message")

    def test_update_node_rejects_unknown_fields(self):
        with self.assertRaises(WorkflowDraftError):
            self.service.update_node(
                self.filename,
                "question_one",
                {"type": "resolution"},
            )

    def test_update_node_rejects_path_traversal(self):
        with self.assertRaises(WorkflowDraftError):
            self.service.update_node(
                "../example.json",
                "question_one",
                {"question": "Unsafe"},
            )

    def test_patch_endpoint_returns_normalized_node(self):
        temporary_service = self.service

        with patch(
            "app.app.WorkflowDraftService",
            return_value=temporary_service,
        ):
            response = app.test_client().patch(
                f"/api/workflow-drafts/{self.filename}/nodes/done",
                json={"title": "Finished", "message": "Saved message"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["node"]["title"], "Finished")
        saved = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["nodes"]["done"]["message"], "Saved message")

    def test_update_settings_persists_without_changing_workflow_id(self):
        updated = self.service.update_settings(
            self.filename,
            {
                "name": "Updated workflow",
                "description": "Updated description",
                "estimated_steps": 7,
                "start_node": "done",
            },
        )

        self.assertEqual(updated["workflow_id"], "example")
        self.assertEqual(updated["name"], "Updated workflow")
        self.assertEqual(updated["estimated_steps"], 7)
        self.assertEqual(updated["start_node"], "done")

    def test_update_settings_rejects_invalid_start_node_and_id_change(self):
        with self.assertRaises(WorkflowDraftError):
            self.service.update_settings(
                self.filename,
                {
                    "name": "Example",
                    "description": "",
                    "estimated_steps": 2,
                    "start_node": "missing",
                },
            )

        with self.assertRaises(WorkflowDraftError):
            self.service.update_settings(
                self.filename,
                {
                    "workflow_id": "changed",
                    "name": "Example",
                    "description": "",
                    "estimated_steps": 2,
                    "start_node": "question_one",
                },
            )

    def test_settings_endpoint_returns_updated_summary_values(self):
        with patch("app.app.WorkflowDraftService", return_value=self.service):
            response = app.test_client().patch(
                f"/api/workflow-drafts/{self.filename}/settings",
                json={
                    "name": "Settings API",
                    "description": "Saved through API",
                    "estimated_steps": 4,
                    "start_node": "done",
                },
            )

        self.assertEqual(response.status_code, 200)
        settings = response.get_json()["settings"]
        self.assertEqual(settings["name"], "Settings API")
        self.assertEqual(settings["start_node"], "done")
        self.assertEqual(settings["start_node_title"], "Done")


if __name__ == "__main__":
    unittest.main()
