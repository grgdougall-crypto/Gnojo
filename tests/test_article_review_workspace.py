import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_review_service import ArticleReviewService
from app.services.workflow_coverage_service import WorkflowCoverageService


def review_article():
    workflow = {
        "workflow_id": "review_test", "name": "Review Test",
        "category": "Desktop Support",
    }
    node = {
        "type": "instruction", "title": "Inspect Startup Apps",
        "instruction": "Open Task Manager and inspect Startup apps.",
    }
    return WorkflowCoverageService().create_article_draft(workflow, "inspect_startup", node)


def review_form(action="save", *, sources="Microsoft Support | https://support.microsoft.com/windows"):
    values = {
        "title": "How to Inspect Startup Apps",
        "category": "Desktop Support",
        "difficulty": "Beginner",
        "estimated_time": "5 minutes",
        "overview": "Review startup applications safely and identify optional high-impact entries.",
        "tags": "windows, task manager, startup, performance",
        "checklist": "Save open work\nOpen Task Manager\nReview Startup apps",
        "common_indicators": "Slow sign-in\nHigh resource use after startup",
        "related_topics": "Task Manager\nWindows performance",
        "commands": "tasklist | Lists running processes without changing them",
        "sources": sources,
        "quiz_question": "What should you disable?",
        "quiz_answers": "Only optional applications you recognize\nEvery Windows process",
        "quiz_correct_answer": "Only optional applications you recognize",
        "review_notes": "Reviewed against Microsoft guidance.",
        "review_action": action,
    }
    for key in ArticleReviewService.CHECKS:
        values[f"review_{key}"] = "on"
    return values


class ArticleReviewServiceTests(unittest.TestCase):
    def test_checklist_removes_number_prefixes(self):
        values = review_form("save")
        values["checklist"] = (
            "1. Save open work\n2) Open Device Manager\nReview the result"
        )
        article = ArticleReviewService().update_from_form(
            review_article(), values
        )
        self.assertEqual(
            article["checklist"],
            ["Save open work", "Open Device Manager", "Review the result"],
        )

    def test_analysis_flags_missing_sources_and_calculates_completeness(self):
        article = review_article()
        analysis = ArticleReviewService().analyze(article)
        self.assertGreaterEqual(analysis["score"], 80)
        self.assertTrue(any(item["kind"] == "sources" for item in analysis["warnings"]))
        self.assertFalse(analysis["can_publish"])

    def test_approval_requires_every_review_check(self):
        article = review_article()
        values = review_form("approve")
        values.pop("review_sources_verified")
        with self.assertRaises(ValueError):
            ArticleReviewService().update_from_form(article, values)

    def test_editing_normalizes_fields_and_approves_valid_article(self):
        article = ArticleReviewService().update_from_form(review_article(), review_form("approve"))
        self.assertEqual(article["review"]["status"], "approved")
        self.assertEqual(article["commands"][0]["command"], "tasklist")
        self.assertEqual(article["sources"][0]["title"], "Microsoft Support")
        self.assertTrue(ArticleReviewService().analyze(article)["can_publish"])

    def test_source_title_may_contain_pipe_separators(self):
        values = review_form(
            "save",
            sources=(
                "Fix audio issues in Windows | Microsoft Support | "
                "https://support.microsoft.com/windows/audio"
            ),
        )
        article = ArticleReviewService().update_from_form(
            review_article(), values
        )
        self.assertEqual(
            article["sources"][0]["title"],
            "Fix audio issues in Windows | Microsoft Support",
        )
        self.assertEqual(
            article["sources"][0]["url"],
            "https://support.microsoft.com/windows/audio",
        )

    def test_approve_and_publish_action_approves_valid_article(self):
        article = ArticleReviewService().update_from_form(
            review_article(), review_form("approve_and_publish")
        )
        self.assertEqual(article["review"]["status"], "approved")


class ArticleReviewWorkspacePageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = KnowledgeRepository(Path(self.temporary.name) / "knowledge_base")
        self.article = review_article()
        self.repository.save_draft(self.article)
        self.repository_patch = patch("app.app.knowledge_repository", self.repository)
        self.repository_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.repository_patch.stop()
        self.temporary.cleanup()

    def test_workspace_renders_editor_preview_warnings_and_controls(self):
        response = self.client.get(f"/knowledge/drafts/{self.article['id']}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Knowledge Review Workspace", html)
        self.assertIn("Review Warnings", html)
        self.assertIn("Technical Review Checklist", html)
        self.assertIn('data-review-view="preview"', html)
        self.assertIn('id="articlePreviewTopics"', html)
        self.assertIn('id="articlePreviewQuiz"', html)
        self.assertIn('id="articlePreviewTags"', html)
        self.assertIn("article_review.js", html)
        self.assertIn("Submit for review", html)
        self.assertNotIn("Publish article", html)

    def test_save_approve_and_publish_lifecycle(self):
        article_id = self.article["id"]
        save = self.client.post(
            f"/knowledge/drafts/{article_id}", data=review_form("save")
        )
        self.assertEqual(save.status_code, 302)
        self.assertEqual(self.repository.get_draft(article_id)["review"]["status"], "draft")

        approve = self.client.post(
            f"/knowledge/drafts/{article_id}", data=review_form("approve")
        )
        self.assertEqual(approve.status_code, 302)
        approved = self.repository.get_draft(article_id)
        self.assertEqual(approved["review"]["status"], "approved")

        ready_page = self.client.get(f"/knowledge/drafts/{article_id}").get_data(as_text=True)
        self.assertIn("Review approved and validation passed", ready_page)

        published = self.client.post(f"/knowledge/drafts/{article_id}/publish")
        self.assertEqual(published.status_code, 302)
        with self.assertRaises(Exception):
            self.repository.get_draft(article_id)
        self.assertEqual(
            self.repository.get_published_article(article_id)["review"]["status"],
            "approved",
        )

    def test_publish_is_blocked_before_approval(self):
        response = self.client.post(
            f"/knowledge/drafts/{self.article['id']}/publish"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Complete validation", response.get_data(as_text=True))

    def test_published_article_can_be_revised_while_remaining_live(self):
        article_id = self.article["id"]
        published = review_article()
        published["checklist"] = [
            "1. Save open work", "2. Inspect Startup apps"
        ]
        published["review"]["status"] = "approved"
        self.repository.save_draft(published, overwrite=True)
        self.repository.publish_article(article_id)

        response = self.client.post(
            f"/knowledge/published/{article_id}/revise"
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/knowledge/drafts/{article_id}", response.headers["Location"])
        self.assertEqual(
            self.repository.get_published_article(article_id)["review"]["status"],
            "approved",
        )
        revision = self.repository.get_draft(article_id)
        self.assertEqual(revision["review"]["status"], "draft")
        self.assertFalse(any(revision["review"]["checks"].values()))
        self.assertEqual(
            revision["checklist"],
            ["Save open work", "Inspect Startup apps"],
        )
        page = self.client.get(
            f"/knowledge/published/{article_id}"
        ).get_data(as_text=True)
        self.assertIn("Revise article", page)

        approve = self.client.post(
            f"/knowledge/drafts/{article_id}", data=review_form("approve")
        )
        self.assertEqual(approve.status_code, 302)
        republish = self.client.post(
            f"/knowledge/drafts/{article_id}/publish"
        )
        self.assertEqual(republish.status_code, 302)
        self.assertEqual(
            self.repository.get_published_article(article_id)["title"],
            "How to Inspect Startup Apps",
        )
        with self.assertRaises(Exception):
            self.repository.get_draft(article_id)

    def test_rejection_requires_review_notes(self):
        values = review_form("reject")
        values["review_notes"] = ""
        response = self.client.post(
            f"/knowledge/drafts/{self.article['id']}", data=values
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Add a review note", response.get_data(as_text=True))

    def test_pending_review_shows_only_contextual_review_actions(self):
        article_id = self.article["id"]
        self.client.post(
            f"/knowledge/drafts/{article_id}", data=review_form("submit")
        )
        html = self.client.get(
            f"/knowledge/drafts/{article_id}"
        ).get_data(as_text=True)
        self.assertIn("Request changes", html)
        self.assertIn("Approve &amp; publish", html)
        self.assertNotIn(">Save draft<", html)
        self.assertNotIn(">Submit for review<", html)

    def test_approve_and_publish_combines_final_review_steps(self):
        article_id = self.article["id"]
        response = self.client.post(
            f"/knowledge/drafts/{article_id}",
            data=review_form("approve_and_publish"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"/knowledge/published/{article_id}", response.headers["Location"]
        )
        self.assertEqual(
            self.repository.get_published_article(article_id)["review"]["status"],
            "approved",
        )


if __name__ == "__main__":
    unittest.main()
