import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app
from app.services.workflow_ai_service import WorkflowAIError, WorkflowAIService
from app.services.workflow_draft_service import WorkflowDraftService


class FailingProvider:
    def generate_workflow_node_suggestion(self, prompt):
        raise RuntimeError("Primary unavailable")


class MaliciousProvider:
    def generate_workflow_node_suggestion(self, prompt):
        return {
            "question": "Is the VPN connected now?",
            "help_text": "Check the connection indicator.",
            "answer_labels": {
                "yes": "Yes, it is connected",
                "invented": "Injected answer",
            },
            "next": "malicious_route",
            "type": "resolution",
            "id": "changed_id",
        }


class WorkflowAITests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "type": "question",
            "question": "Did it connect?",
            "help_text": "Look at the client.",
            "answers": {
                "yes": {"label": "Yes", "next": "done"},
                "no": {"label": "No", "next": "retry"},
            },
        }

    def test_suggestion_falls_back_and_strips_structural_changes(self):
        service = WorkflowAIService(
            providers=[
                ("Primary", FailingProvider()),
                ("Fallback", MaliciousProvider()),
            ]
        )

        result = service.improve_node("question_one", self.node, "clarity")

        self.assertEqual(result["provider"], "Fallback")
        self.assertEqual(result["proposed"]["question"], "Is the VPN connected now?")
        self.assertEqual(set(result["proposed"]), {"question", "help_text", "answer_labels"})
        self.assertEqual(set(result["proposed"]["answer_labels"]), {"yes", "no"})
        self.assertEqual(result["proposed"]["answer_labels"]["no"], "No")
        self.assertNotIn("next", result["proposed"])

    def test_unknown_style_is_rejected_before_provider_call(self):
        service = WorkflowAIService(providers=[])
        with self.assertRaises(WorkflowAIError):
            service.improve_node("question_one", self.node, "unsafe_style")

    def test_improvement_endpoint_uses_saved_node(self):
        workflow = {
            "workflow_id": "ai_test",
            "name": "AI test",
            "start_node": "question_one",
            "nodes": {"question_one": self.node},
        }
        suggestion = {
            "style": "clarity",
            "provider": "Test provider",
            "original": {"question": "Did it connect?"},
            "proposed": {"question": "Is the VPN connected?"},
        }

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "draft.json").write_text(json.dumps(workflow), encoding="utf-8")
            draft_service = WorkflowDraftService(directory)
            ai_service = Mock()
            ai_service.improve_node.return_value = suggestion

            with patch("app.app.WorkflowDraftService", return_value=draft_service), patch(
                "app.app.WorkflowAIService", return_value=ai_service
            ):
                response = app.test_client().post(
                    "/api/workflow-drafts/draft.json/nodes/question_one/improve",
                    json={"style": "clarity"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "Test provider")
        ai_service.improve_node.assert_called_once_with(
            "question_one",
            self.node,
            "clarity",
        )


if __name__ == "__main__":
    unittest.main()
