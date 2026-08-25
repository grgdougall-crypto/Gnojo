import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)


def valid_workflow():
    return {
        "workflow_id": "publish_test",
        "name": "Publish test",
        "start_node": "start",
        "nodes": {
            "start": {
                "type": "instruction",
                "title": "Start",
                "instruction": "Begin the test",
                "next": "done",
            },
            "done": {
                "type": "resolution",
                "title": "Done",
                "message": "Test complete",
            },
        },
    }


class WorkflowPublicationTests(unittest.TestCase):
    def test_publication_success_closes_dialog_announces_version_and_restores_focus(self):
        root = Path(__file__).resolve().parents[1]
        editor = (root / "app" / "templates" / "workflow_editor.html").read_text(encoding="utf-8")
        scripts = (root / "app" / "templates" / "workflow" / "_workflow_scripts.html").read_text(encoding="utf-8")
        self.assertIn('id="workflowPublicationSuccess"', editor)
        self.assertIn("data-a11y-live", editor)
        self.assertIn("publicationDialog.close();", scripts)
        self.assertIn('element("versionHistoryButton").focus()', scripts)
        self.assertIn("successBanner.hidden = false", scripts)
        self.assertIn("validationPassed = false", scripts)
        self.assertIn("updatePublishAvailability();", scripts)

    def test_editor_badge_identifies_editable_draft_and_published_relationship(self):
        scripts = (Path(__file__).resolve().parents[1] / "app" / "templates" / "workflow"
                   / "_workflow_scripts.html").read_text(encoding="utf-8")
        self.assertIn('"Editable draft · Not published"', scripts)
        self.assertIn("`Editable draft · Published v${publicationStatus.current_version}`", scripts)
        self.assertIn(
            "`Editable draft changes · Published v${publicationStatus.current_version}`",
            scripts,
        )
        self.assertNotIn("`Published · v${publicationStatus.current_version}`", scripts)

    def test_publish_creates_immutable_numbered_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowPublicationService(directory)
            workflow = valid_workflow()
            first = service.publish(workflow, "draft.json", "Initial release")
            workflow["nodes"]["done"]["message"] = "Updated release"
            second = service.publish(workflow, "draft.json", "Updated release")

            first_snapshot = json.loads(
                (Path(directory) / "publish_test" / "v0001.json").read_text(encoding="utf-8")
            )
            second_snapshot = json.loads(
                (Path(directory) / "publish_test" / "v0002.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first["current_version"], 1)
        self.assertEqual(second["current_version"], 2)
        self.assertEqual(first_snapshot["workflow"]["nodes"]["done"]["message"], "Test complete")
        self.assertEqual(second_snapshot["workflow"]["nodes"]["done"]["message"], "Updated release")

    def test_publish_rejects_invalid_and_duplicate_content(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowPublicationService(directory)
            workflow = valid_workflow()
            service.publish(workflow, "draft.json")

            with self.assertRaises(WorkflowPublicationError):
                service.publish(deepcopy(workflow), "draft.json")

            workflow["nodes"]["start"]["next"] = "missing"
            with self.assertRaises(WorkflowPublicationError):
                service.publish(workflow, "draft.json")

    def test_publication_api_returns_history(self):
        with tempfile.TemporaryDirectory() as drafts_directory, tempfile.TemporaryDirectory() as publications_directory:
            workflow = valid_workflow()
            Path(drafts_directory, "draft.json").write_text(json.dumps(workflow), encoding="utf-8")
            draft_service = WorkflowDraftService(drafts_directory)
            publication_service = WorkflowPublicationService(publications_directory)

            with patch("app.app.WorkflowDraftService", return_value=draft_service), patch(
                "app.app.WorkflowPublicationService", return_value=publication_service
            ):
                client = app.test_client()
                published = client.post(
                    "/api/workflow-drafts/draft.json/publication",
                    json={"label": "Initial release"},
                )
                status = client.get("/api/workflow-drafts/draft.json/publication")

        self.assertEqual(published.status_code, 201)
        self.assertEqual(published.get_json()["current_version"], 1)
        self.assertFalse(published.get_json()["has_unpublished_changes"])
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["versions"][0]["label"], "Initial release")


if __name__ == "__main__":
    unittest.main()
