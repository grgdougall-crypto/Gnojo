import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_draft_service import WorkflowDraftService


class WorkflowExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workflow = {
            "workflow_id": "export_test",
            "name": "Export Test Workflow",
            "start_node": "q_start",
            "estimated_steps": 2,
            "nodes": {
                "q_start": {"type": "question", "title": "Start", "question": "Ready?", "answers": {"yes": {"label": "Yes", "next": "done"}}},
                "done": {"type": "resolution", "title": "Complete", "message": "Finished."},
            },
        }
        Path(self.temp.name, "draft.json").write_text(json.dumps(self.workflow), encoding="utf-8")
        self.service = WorkflowDraftService(self.temp.name)
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, export_format):
        with patch("app.app.WorkflowDraftService", return_value=self.service):
            return self.client.get(f"/api/workflow-drafts/draft.json/export/{export_format}")

    def test_json_markdown_and_pdf_downloads(self):
        json_response = self.request("json")
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json.loads(json_response.data)["workflow_id"], "export_test")
        self.assertIn("export-test-workflow.json", json_response.headers["Content-Disposition"])

        markdown = self.request("markdown")
        self.assertIn(b"# Export Test Workflow", markdown.data)
        self.assertIn(b"Ready?", markdown.data)
        self.assertIn("export-test-workflow.md", markdown.headers["Content-Disposition"])

        pdf = self.request("pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-1.4"))
        self.assertIn("export-test-workflow.pdf", pdf.headers["Content-Disposition"])

    def test_unknown_format_is_rejected(self):
        response = self.request("docx")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose JSON", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
