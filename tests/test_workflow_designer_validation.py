import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_validation_service import WorkflowValidationService


class WorkflowDesignerValidationTests(unittest.TestCase):
    def test_editor_renders_simulator_with_start_node(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "simulator.json"
            path.write_text(
                json.dumps(
                    {
                        "workflow_id": "simulator",
                        "name": "Simulator workflow",
                        "estimated_steps": 2,
                        "start_node": "start_here",
                        "nodes": {
                            "start_here": {
                                "type": "instruction",
                                "title": "Start here",
                                "instruction": "Begin",
                                "next": "done",
                            },
                            "done": {
                                "type": "resolution",
                                "title": "Done",
                                "message": "Complete",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = WorkflowDraftService(directory)

            with patch("app.app.WorkflowDraftService", return_value=service):
                response = app.test_client().get(
                    "/workflow-editor/simulator.json"
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="simulateWorkflowButton"', html)
        self.assertIn('id="workflowSimulatorDialog"', html)
        self.assertIn('data-start-node="start_here"', html)
        self.assertIn('data-estimated-steps="2"', html)
        self.assertIn(
            "Math.max(estimatedSteps, simulatorState.path.length)", html
        )
        self.assertIn('id="workflowSettingsDialog"', html)
        self.assertIn('id="workflowSettingsButton"', html)
        self.assertIn('data-settings-url=', html)
        self.assertIn('id="workflowAIDialog"', html)
        self.assertIn('id="improveNodeButton"', html)
        self.assertIn('data-node-improve-url=', html)

    def test_malformed_nodes_return_errors_instead_of_crashing(self):
        workflow = {
            "workflow_id": "broken",
            "name": "Broken workflow",
            "start_node": "question_one",
            "nodes": {
                "question_one": {
                    "type": "question",
                    "question": "Continue?",
                    "answers": [],
                },
                "not_an_object": "broken",
            },
        }

        result = WorkflowValidationService().validate(workflow)

        self.assertFalse(result["is_valid"])
        self.assertTrue(any("must have answers" in error for error in result["errors"]))
        self.assertTrue(any("must be an object" in error for error in result["errors"]))

    def test_validation_endpoint_returns_clickable_node_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text(
                json.dumps(
                    {
                        "workflow_id": "broken",
                        "name": "Broken workflow",
                        "start_node": "step_one",
                        "nodes": {
                            "step_one": {
                                "type": "instruction",
                                "title": "Step one",
                                "instruction": "Do the thing",
                                "next": "missing_node",
                            },
                            "done": {
                                "type": "resolution",
                                "title": "Done",
                                "message": "Finished",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = WorkflowDraftService(directory)

            with patch("app.app.WorkflowDraftService", return_value=service):
                response = app.test_client().get(
                    "/api/workflow-drafts/broken.json/validation"
                )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertFalse(result["is_valid"])
        issue = next(item for item in result["issues"] if item["level"] == "error")
        self.assertEqual(issue["node_id"], "step_one")


if __name__ == "__main__":
    unittest.main()
