import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)


def generated_workflow(*, target="missing_workflow"):
    return {
        "workflow_id": "generated_sign_in_problem",
        "name": "Windows Sign-In Problem",
        "description": "Troubleshoot Windows sign-in problems.",
        "platform": "Windows",
        "difficulty": "Intermediate",
        "size": "Small",
        "estimated_steps": 1,
        "start_node": "advanced_support",
        "nodes": {
            "advanced_support": {
                "type": "transition",
                "title": "Advanced Windows Sign-In Diagnostics",
                "message": "Advanced support is required.",
                "next_workflow": target,
            }
        },
    }


class WorkflowBuilderPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        self.previous_testing = app.testing
        self.previous_auth_bypass = app.config.get("AUTH_TEST_BYPASS")
        app.testing = True
        app.config["AUTH_TEST_BYPASS"] = True
        self.client = app.test_client()

    def tearDown(self):
        app.testing = self.previous_testing
        app.config["AUTH_TEST_BYPASS"] = self.previous_auth_bypass
        self.temporary.cleanup()

    @staticmethod
    def form():
        return {
            "workflow_name": "Windows Sign-In Problem",
            "description": "Troubleshoot Windows sign-in problems.",
            "platform": "Windows",
            "difficulty": "Intermediate",
            "size": "Small",
        }

    def test_invalid_handoff_persists_for_designer_repair_but_cannot_publish(self):
        engine = Mock()
        engine.generate_workflow.return_value = generated_workflow()

        with (
            patch("app.app.WorkflowGenerationEngine", return_value=engine),
            patch("app.app.WorkflowDraftService", return_value=self.drafts),
        ):
            response = self.client.post(
                "/workflow-builder", data=self.form(), follow_redirects=False
            )
            result = self.client.get(response.headers["Location"])
            designer = self.client.get(
                "/workflow-editor/generated_sign_in_problem.json"
            )
            validation = self.client.get(
                "/api/workflow-drafts/generated_sign_in_problem.json/validation",
                headers={"Accept": "application/json"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "/workflow-builder/result/generated_sign_in_problem.json",
        )
        self.assertIsNotNone(
            self.drafts.get_draft("generated_sign_in_problem.json")
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Open in Workflow Designer", result.data)
        self.assertIn(b"saved as an editable draft", result.data)
        self.assertEqual(designer.status_code, 200)
        self.assertEqual(validation.status_code, 200)
        validation_result = validation.get_json()
        self.assertFalse(validation_result["is_valid"])
        self.assertTrue(any(
            "references unavailable workflow 'missing_workflow'" in item["message"]
            for item in validation_result["issues"]
        ))

        with self.assertRaisesRegex(
            WorkflowPublicationError,
            "must pass validation",
        ):
            WorkflowPublicationService(
                self.root / "app" / "workflow_publications"
            ).publish(
                self.drafts.get_draft("generated_sign_in_problem.json"),
                source_filename="generated_sign_in_problem.json",
            )

    def test_unparseable_provider_output_remains_rejected_without_a_draft(self):
        engine = Mock()
        engine.generate_workflow.side_effect = RuntimeError(
            "OpenAI returned invalid workflow JSON."
        )

        with (
            patch("app.app.WorkflowGenerationEngine", return_value=engine),
            patch("app.app.WorkflowDraftService", return_value=self.drafts),
        ):
            response = self.client.post("/workflow-builder", data=self.form())

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"OpenAI returned invalid workflow JSON.", response.data)
        self.assertEqual(self.drafts.list_drafts(), [])

    def test_structurally_incomplete_workflow_remains_unsaved(self):
        engine = Mock()
        engine.generate_workflow.return_value = {
            "workflow_id": "incomplete",
            "name": "Incomplete",
            "nodes": {},
        }

        with (
            patch("app.app.WorkflowGenerationEngine", return_value=engine),
            patch("app.app.WorkflowDraftService", return_value=self.drafts),
        ):
            response = self.client.post("/workflow-builder", data=self.form())

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"structure is incomplete or unsafe to load", response.data)
        self.assertEqual(self.drafts.list_drafts(), [])

    def test_valid_generated_workflow_keeps_existing_review_path(self):
        workflow = generated_workflow(target="printer")
        engine = Mock()
        engine.generate_workflow.return_value = workflow

        with (
            patch("app.app.WorkflowGenerationEngine", return_value=engine),
            patch("app.app.WorkflowDraftService", return_value=self.drafts),
        ):
            response = self.client.post(
                "/workflow-builder", data=self.form(), follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(
            self.drafts.get_draft("generated_sign_in_problem.json")
        )
        self.assertIn(b"Workflow passed validation", response.data)
        self.assertIn(b"Review and Prepare to Publish", response.data)
        self.assertNotIn(b"Open in Workflow Designer", response.data)


if __name__ == "__main__":
    unittest.main()
