import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_catalog_service import WorkflowCatalogError
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_validation_service import WorkflowValidationService


class WorkflowDesignerValidationTests(unittest.TestCase):
    def setUp(self):
        self.previous_testing = app.testing
        self.previous_auth_bypass = app.config.get("AUTH_TEST_BYPASS")
        app.testing = True
        app.config["AUTH_TEST_BYPASS"] = True

    def tearDown(self):
        app.testing = self.previous_testing
        app.config["AUTH_TEST_BYPASS"] = self.previous_auth_bypass

    def test_missing_cross_workflow_target_blocks_safe_publication(self):
        workflow = {
            "workflow_id": "microphone_fixture",
            "name": "Microphone Not Working",
            "start_node": "advanced_support",
            "nodes": {
                "advanced_support": {
                    "type": "transition",
                    "title": "Advanced Diagnostics Recommended",
                    "message": "Further support investigation is required.",
                    "next_workflow": "windows_advanced_audio_diagnostics",
                }
            },
        }

        result = WorkflowValidationService().validate(
            workflow, available_workflow_ids={"printer", "no_sound"}
        )

        self.assertFalse(result["is_valid"])
        self.assertTrue(any(
            "references unavailable workflow 'windows_advanced_audio_diagnostics'"
            in error
            for error in result["errors"]
        ))
        self.assertEqual(result["quality"]["checks"]["handoffs"], "ERROR")
        self.assertEqual(result["quality"]["overall_status"], "ERROR")

    def test_existing_cross_workflow_target_remains_valid(self):
        workflow = {
            "workflow_id": "handoff_fixture",
            "name": "Valid handoff",
            "start_node": "continue_to_printer",
            "nodes": {
                "continue_to_printer": {
                    "type": "transition",
                    "title": "Continue to Printer",
                    "message": "Use the existing printer workflow.",
                    "next_workflow": "printer",
                }
            },
        }

        result = WorkflowValidationService().validate(
            workflow, available_workflow_ids={"printer", "no_sound"}
        )

        self.assertTrue(result["is_valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["quality"]["checks"]["handoffs"], "PASS")

    def test_microphone_advanced_diagnostics_is_a_safe_terminal_resolution(self):
        workflow = {
            "workflow_id": "microphone_fixture",
            "name": "Microphone Not Working",
            "start_node": "transition_advanced_diagnostics",
            "nodes": {
                "transition_advanced_diagnostics": {
                    "type": "resolution",
                    "title": "Advanced Diagnostics Recommended",
                    "message": (
                        "The standard troubleshooting steps did not resolve the "
                        "microphone issue. Further IT or support investigation may "
                        "include driver review, Device Manager checks, or hardware "
                        "diagnostics."
                    ),
                }
            },
        }

        result = WorkflowValidationService().validate(workflow)

        self.assertTrue(result["is_valid"])
        node = workflow["nodes"]["transition_advanced_diagnostics"]
        self.assertEqual(node["type"], "resolution")
        self.assertNotIn("next_workflow", node)

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
        self.assertIn('id="continueToPublishButton"', html)
        self.assertIn("Continue to publish", html)
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
        self.assertIn(
            "Your reviewer session expired. Sign in again, then retry validation.",
            html,
        )
        self.assertIn(
            "Validation returned an unexpected server response.",
            html,
        )

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

    def test_validation_endpoint_returns_json_when_catalog_resolution_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            path.write_text(
                json.dumps(
                    {
                        "workflow_id": "handoff",
                        "name": "Handoff workflow",
                        "start_node": "continue_elsewhere",
                        "nodes": {
                            "continue_elsewhere": {
                                "type": "transition",
                                "title": "Continue elsewhere",
                                "message": "Continue in the target workflow.",
                                "next_workflow": "target",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = WorkflowDraftService(directory)

            with (
                patch("app.app.WorkflowDraftService", return_value=service),
                patch(
                    "app.services.workflow_catalog_service."
                    "WorkflowCatalogService.catalog",
                    side_effect=WorkflowCatalogError("ambiguous catalog"),
                ),
            ):
                response = app.test_client().get(
                    "/api/workflow-drafts/handoff.json/validation",
                    headers={"Accept": "application/json"},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "error": (
                    "Workflow validation could not resolve the authoritative "
                    "workflow catalog."
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
