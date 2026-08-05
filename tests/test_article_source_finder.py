import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.app import app
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_source_finder_service import (
    ArticleSourceFinderError,
    ArticleSourceFinderService,
)


class FakeSourceProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.prompt = ""

    def find_authoritative_sources(self, prompt):
        self.prompt = prompt
        if self.error:
            raise self.error
        return self.result


def draft_article():
    return {
        "id": "bluetooth-driver",
        "title": "How to Install the Approved Bluetooth Driver",
        "category": "Desktop Support",
        "overview": "Install a Bluetooth driver from an approved source.",
        "checklist": ["Use Windows Update or the computer manufacturer's support site."],
        "related_topics": ["Bluetooth", "Windows drivers"],
        "sources": [],
        "commands": [],
        "common_indicators": [],
        "quiz": [],
        "review": {"status": "draft", "checks": {}, "notes": []},
    }


class ArticleSourceFinderServiceTests(unittest.TestCase):
    def test_returns_https_primary_source_candidates(self):
        provider = FakeSourceProvider({"sources": [{
            "title": "Update drivers through Device Manager in Windows",
            "url": "https://support.microsoft.com/windows/update-drivers",
            "publisher": "Microsoft Support",
            "reason": "Supports the Windows driver installation and update guidance in this draft.",
        }]})
        result = ArticleSourceFinderService(
            providers=[("Gemini Search", provider)],
            redirect_resolver=lambda url: url,
        ).find(draft_article())
        self.assertEqual(result["provider"], "Gemini Search")
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertIn("Bluetooth Driver", provider.prompt)

    def test_rejects_non_https_or_unusable_results(self):
        provider = FakeSourceProvider({"sources": [{
            "title": "Untrusted result",
            "url": "http://example.com/post",
            "reason": "This is not an acceptable secure primary source for the article.",
        }]})
        with self.assertRaises(ArticleSourceFinderError):
            ArticleSourceFinderService(providers=[("Search", provider)]).find(draft_article())

    def test_rejects_vendor_homepage_disguised_as_article(self):
        provider = FakeSourceProvider({"sources": [{
            "title": "How to Download and Install Bluetooth Driver for Windows",
            "url": "https://www.dell.com/en-us",
            "publisher": "Dell",
            "reason": "Claims to provide device-specific Bluetooth driver installation guidance.",
        }]})
        with self.assertRaises(ArticleSourceFinderError):
            ArticleSourceFinderService(providers=[("Search", provider)]).find(draft_article())

    def test_resolves_grounding_redirect_before_returning_source(self):
        provider = FakeSourceProvider({"sources": [{
            "title": "Update Bluetooth drivers in Windows",
            "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token",
            "publisher": "Microsoft Support",
            "reason": "Supports the Windows Bluetooth driver update instructions in this draft.",
        }]})
        service = ArticleSourceFinderService(
            providers=[("Gemini Search", provider)],
            redirect_resolver=lambda _url: "https://support.microsoft.com/windows/update-bluetooth-drivers",
        )
        result = service.find(draft_article())
        self.assertEqual(
            result["suggestions"][0]["url"],
            "https://support.microsoft.com/windows/update-bluetooth-drivers",
        )

    @patch("app.services.article_source_finder_service.requests.get")
    def test_grounding_redirect_rejects_soft_404_page(self, get):
        response = MagicMock()
        response.url = "https://support.microsoft.com/windows/missing-article"
        response.iter_content.return_value = [b"<html><h1>Sorry, page not found</h1></html>"]
        get.return_value = response
        service = ArticleSourceFinderService()
        with self.assertRaises(ArticleSourceFinderError):
            service._resolve_search_redirect(
                "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token"
            )

    @patch("app.services.article_source_finder_service.requests.get")
    def test_rejects_vendor_unavailable_document_page(self, get):
        response = MagicMock()
        response.url = "https://www.dell.com/support/kbdoc/en-us/unavailable"
        response.iter_content.return_value = [
            b"<html>The chosen document is not currently available. Please try again later.</html>"
        ]
        get.return_value = response
        service = ArticleSourceFinderService()
        with self.assertRaises(ArticleSourceFinderError):
            service._resolve_search_redirect(
                "https://www.dell.com/support/kbdoc/en-us/unavailable"
            )


class ArticleSourceFinderEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = KnowledgeRepository(Path(self.temporary.name))
        self.repository.save_draft(draft_article())
        self.repository_patch = patch("app.app.knowledge_repository", self.repository)
        self.repository_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.repository_patch.stop()
        self.temporary.cleanup()

    @patch("app.app.ArticleSourceFinderService.find")
    def test_endpoint_and_workspace_render_reviewable_suggestions(self, find):
        find.return_value = {"provider": "Gemini Search", "suggestions": [{
            "title": "Official source",
            "url": "https://support.microsoft.com/example",
            "publisher": "Microsoft",
            "reason": "Directly supports the draft's driver installation guidance.",
        }]}
        response = self.client.post("/api/knowledge/drafts/bluetooth-driver/source-suggestions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "Gemini Search")

        html = self.client.get("/knowledge/drafts/bluetooth-driver").get_data(as_text=True)
        self.assertIn("Find authoritative sources", html)
        self.assertIn("articleSourceSuggestions", html)
        self.assertIn("source-suggestions", html)


if __name__ == "__main__":
    unittest.main()
