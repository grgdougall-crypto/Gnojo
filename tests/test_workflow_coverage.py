import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.workflow_coverage_service import WorkflowCoverageService
from app.services.workflow_draft_service import WorkflowDraftService


def coverage_workflow():
    return {
        "workflow_id": "coverage_test",
        "name": "Coverage Test",
        "category": "Desktop Support",
        "platform": "Windows",
        "estimated_steps": 2,
        "start_node": "check_step",
        "nodes": {
            "check_step": {
                "type": "instruction",
                "title": "Check Startup Applications",
                "instruction": "Open Task Manager and review Startup apps.",
                "next": "done",
            },
            "done": {"type": "resolution", "title": "Complete", "message": "Finished."},
        },
    }


class WorkflowCoverageServiceTests(unittest.TestCase):
    def test_help_text_is_contextual_and_article_is_valid(self):
        workflow = coverage_workflow()
        node = workflow["nodes"]["check_step"]
        service = WorkflowCoverageService()
        help_text = service.generate_help_text(node)
        self.assertIn("check startup applications", help_text.lower())
        self.assertIn("startup impact", help_text)
        self.assertIn("security", help_text)
        article = service.create_article_draft(workflow, "check_step", node)
        self.assertEqual(article["id"], "coverage-test-check-step")
        self.assertEqual(ArticleValidator.validate(article), [])
        self.assertEqual(article["review"]["status"], "draft")
        self.assertEqual(article["generation"]["provider"], "Gnojo Coverage Assistant")
        self.assertGreaterEqual(len(article["tags"]), 3)
        self.assertIn("task manager", article["tags"])

    def test_help_text_uses_diagnostic_evidence_for_ip_configuration(self):
        help_text = WorkflowCoverageService().generate_help_text({
            "type": "instruction",
            "title": "Inspect the IP Configuration",
            "instruction": "Open Command Prompt and run ipconfig /all.",
        })

        self.assertIn("IPv4 address", help_text)
        self.assertIn("default gateway", help_text)
        self.assertIn("DNS servers", help_text)
        self.assertIn("without changing network settings", help_text)

    def test_help_text_distinguishes_dns_and_gateway_diagnostics(self):
        service = WorkflowCoverageService()
        dns_help = service.generate_help_text({
            "type": "instruction",
            "title": "Test DNS Resolution",
            "instruction": "Run nslookup example.com and confirm that a DNS server returns an address.",
        })
        gateway_help = service.generate_help_text({
            "type": "instruction",
            "title": "Test the Default Gateway",
            "instruction": "Ping the default gateway and record whether it responds.",
        })

        self.assertIn("DNS server used", dns_help)
        self.assertIn("timeout or nonexistent domain", dns_help)
        self.assertNotIn("DHCP status", dns_help)
        self.assertIn("packet loss", gateway_help)
        self.assertIn("local reachability only", gateway_help)
        self.assertIn("name resolution", gateway_help)
        self.assertNotIn("DHCP status", gateway_help)

    def test_help_text_uses_question_and_resolution_content(self):
        service = WorkflowCoverageService()

        question_help = service.generate_help_text({
            "type": "question",
            "question": "Did the default gateway respond?",
        })
        resolution_help = service.generate_help_text({
            "type": "resolution",
            "title": "DNS Resolution Problem",
            "message": "The gateway responds, but name resolution failed.",
        })

        self.assertIn("Did the default gateway respond", question_help)
        self.assertIn("name resolution failed", resolution_help)

    def test_article_generation_rejects_non_instruction_node(self):
        workflow = coverage_workflow()
        with self.assertRaises(ValueError):
            WorkflowCoverageService().create_article_draft(
                workflow, "done", workflow["nodes"]["done"]
            )


class WorkflowCoverageEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.drafts = WorkflowDraftService(root / "workflow_drafts")
        self.repository = KnowledgeRepository(root / "knowledge_base")
        self.filename = self.drafts.save_draft(coverage_workflow())
        self.draft_patch = patch("app.app.WorkflowDraftService", return_value=self.drafts)
        self.repository_patch = patch("app.app.knowledge_repository", self.repository)
        self.draft_patch.start()
        self.repository_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.repository_patch.stop()
        self.draft_patch.stop()
        self.temporary.cleanup()

    @patch("app.app.WorkflowHelpTextService.suggest")
    def test_help_text_endpoint_previews_then_accepts(self, suggest):
        suggested_text = (
            "Review the Startup apps list and record each item's publisher and startup impact. "
            "An unfamiliar or high-impact entry is evidence for follow-up, but it does not by itself prove malicious activity."
        )
        suggest.return_value = {
            "help_text": suggested_text,
            "provider": "Gemini",
            "used_fallback": False,
            "quality_checks": ["Uses the selected node's workflow context"],
        }
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/help-text",
            json={},
        )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["provider"], "Gemini")
        unchanged = self.drafts.get_draft(self.filename)
        self.assertNotIn("help_text", unchanged["nodes"]["check_step"])

        accepted = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/help-text",
            json={"action": "accept", "help_text": suggested_text},
        )
        self.assertEqual(accepted.status_code, 200)
        accepted_result = accepted.get_json()
        self.assertTrue(accepted_result["accepted"])
        saved = self.drafts.get_draft(self.filename)
        self.assertEqual(saved["nodes"]["check_step"]["help_text"], suggested_text)

    def test_article_endpoint_creates_draft_and_links_node(self):
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/article"
        )
        self.assertEqual(response.status_code, 201)
        result = response.get_json()
        self.assertEqual(result["article_id"], "coverage-test-check-step")
        self.assertEqual(result["review_url"], "/knowledge/drafts/coverage-test-check-step")
        article = self.repository.get_draft(result["article_id"])
        self.assertEqual(article["title"], "How to Check Startup Applications")
        self.assertEqual(article["workflow_origin"]["filename"], self.filename)
        self.assertEqual(article["workflow_origin"]["node_id"], "check_step")
        workflow = self.drafts.get_draft(self.filename)
        self.assertEqual(
            workflow["nodes"]["check_step"]["knowledge_article"],
            "coverage-test-check-step",
        )
        repeated = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/article"
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.get_json()["created"])

    def test_published_workflow_article_returns_to_originating_node(self):
        created = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/check_step/coverage/article"
        ).get_json()
        article = self.repository.get_draft(created["article_id"])
        response = self.client.post(
            f"/knowledge/drafts/{article['id']}",
            data={
                "title": article["title"],
                "category": article["category"],
                "difficulty": article["difficulty"],
                "estimated_time": article["estimated_time"],
                "overview": article["overview"],
                "tags": ", ".join(article["tags"]),
                "checklist": "\n".join(article["checklist"]),
                "common_indicators": "\n".join(article["common_indicators"]),
                "related_topics": "\n".join(article["related_topics"]),
                "commands": "",
                "sources": "Microsoft guidance | https://support.microsoft.com/windows",
                "quiz_question": article["quiz"][0]["question"],
                "quiz_answers": "\n".join(article["quiz"][0]["answers"]),
                "quiz_correct_answer": article["quiz"][0]["correct_answer"],
                "review_technical_accuracy": "on",
                "review_user_safety": "on",
                "review_sources_verified": "on",
                "review_commands_reviewed": "on",
                "review_notes": "Verified for publication.",
                "review_action": "approve_and_publish",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/workflow-editor/{self.filename}", response.headers["Location"])
        self.assertIn("node=check_step", response.headers["Location"])
        self.assertIn(f"article_published={article['id']}", response.headers["Location"])

    def test_editor_renders_coverage_controls(self):
        html = self.client.get(f"/workflow-editor/{self.filename}").get_data(as_text=True)
        self.assertIn("Content Coverage Assistant", html)
        self.assertIn("generateHelpTextButton", html)
        self.assertIn("createArticleDraftButton", html)
        self.assertIn("helpTextPreview", html)
        self.assertIn("Nothing is saved until you accept it", html)
        self.assertIn("data-help-text-url", html)

    def test_stale_editor_node_returns_refresh_guidance(self):
        response = self.client.post(
            f"/api/workflow-drafts/{self.filename}/nodes/old_node_id/coverage/help-text"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Refresh the Workflow Designer", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
