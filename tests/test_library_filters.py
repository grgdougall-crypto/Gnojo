import unittest
from unittest.mock import patch

from app.app import app
from app.repositories.command_repository import CommandRepository


class LibraryFilterTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.articles = [
            {
                "id": "dns-basics", "title": "DNS Basics",
                "overview": "Resolve names safely.", "category": "Networking",
                "difficulty": "Beginner", "tags": ["DNS"],
            },
            {
                "id": "vpn-guide", "title": "VPN Guide",
                "overview": "Check the secure tunnel.", "category": "Networking",
                "difficulty": "Intermediate", "tags": ["VPN"],
            },
            {
                "id": "printer-guide", "title": "Printer Guide",
                "overview": "Inspect the print queue.", "category": "Printing",
                "difficulty": "Beginner", "tags": ["printer"],
            },
        ]
        self.commands = [
            {
                "id": "ipconfig", "name": "ipconfig", "title": "Inspect IP Configuration",
                "summary": "Review DNS and adapter settings.", "category": "Networking",
                "shell": "Command Prompt", "platforms": ["Windows"], "tags": ["DNS"],
            },
            {
                "id": "resolve-dns", "name": "Resolve-DnsName", "title": "Resolve a DNS Name",
                "summary": "Test name resolution.", "category": "Networking",
                "shell": "PowerShell", "platforms": ["Windows"], "tags": ["DNS"],
            },
            {
                "id": "get-printer", "name": "Get-Printer", "title": "Inspect Printers",
                "summary": "List configured printers.", "category": "Printing",
                "shell": "PowerShell", "platforms": ["Windows"], "tags": ["printer"],
            },
        ]

    def test_published_search_and_category_compose_with_complete_counts(self):
        with patch("app.app.knowledge_repository.get_published", return_value=self.articles):
            response = self.client.get("/knowledge/published?q=dns&category=networking")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("DNS Basics", html)
        self.assertNotIn("VPN Guide", html)
        self.assertNotIn("Printer Guide", html)
        self.assertIn("Networking (2)", html)
        self.assertIn("Printing (1)", html)
        self.assertIn('value="dns"', html)
        self.assertIn('value="Networking" selected', html)
        self.assertIn("Showing 1 of 3 published articles.", html)
        self.assertIn("Back to Knowledge Center", html)
        self.assertIn('aria-label="View article: DNS Basics"', html)
        self.assertIn('aria-label="Manage article in Knowledge Integrity: DNS Basics"', html)
        self.assertIn("Manage in Integrity", html)
        self.assertIn(
            "return_to=/knowledge/published?q%3Ddns%26category%3Dnetworking",
            html,
        )

    def test_command_search_and_category_compose_case_insensitively(self):
        with patch("app.app.command_repository.get_all", return_value=self.commands):
            response = self.client.get("/commands?q=DNS&category=NETWORKING")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Inspect IP Configuration", html)
        self.assertIn("Resolve a DNS Name", html)
        self.assertNotIn("Inspect Printers", html)
        self.assertIn("Networking (2)", html)
        self.assertIn("Printing (1)", html)
        self.assertIn('value="DNS"', html)
        self.assertIn('value="Networking" selected', html)
        self.assertIn("Showing 2 of 3 commands.", html)
        self.assertIn("Back to Knowledge Center", html)
        self.assertIn('aria-label="View command: Inspect IP Configuration"', html)
        self.assertIn(
            "return_to=/commands?q%3DDNS%26category%3DNETWORKING",
            html,
        )

    def test_details_return_to_filtered_lists_and_reject_external_targets(self):
        article = self.articles[0]
        with (
            patch("app.app.knowledge_repository.resolve_published_article", return_value=article),
            patch("app.app.relationship_service.related_commands_for_article", return_value=[]),
        ):
            article_html = self.client.get(
                "/knowledge/published/dns-basics?return_to=%2Fknowledge%2Fpublished%3Fq%3Ddns%26category%3DNetworking"
            ).get_data(as_text=True)
            unsafe_article = self.client.get(
                "/knowledge/published/dns-basics?return_to=https%3A%2F%2Fevil.example"
            ).get_data(as_text=True)
        self.assertIn('href="/knowledge/published?q=dns&amp;category=Networking"', article_html)
        self.assertIn("Back to Published Articles", article_html)
        self.assertIn('href="/knowledge/published"', unsafe_article)
        self.assertNotIn("evil.example", unsafe_article)

        command = CommandRepository().get("ipconfig")
        with (
            patch("app.app.command_repository.get", return_value=command),
            patch("app.app.relationship_service.related_articles_for_command", return_value=[]),
            patch("app.app.relationship_service.related_commands_for_command", return_value=[]),
            patch("app.app.explanation_service.explain_command", return_value={}),
        ):
            command_html = self.client.get(
                "/commands/ipconfig?return_to=%2Fcommands%3Fq%3DDNS%26category%3DNetworking"
            ).get_data(as_text=True)
            unsafe_command = self.client.get(
                "/commands/ipconfig?return_to=%2F%2Fevil.example%2Fcommands"
            ).get_data(as_text=True)
        self.assertIn('href="/commands?q=DNS&amp;category=Networking"', command_html)
        self.assertIn("Back to Command Library", command_html)
        self.assertIn('href="/commands"', unsafe_command)
        self.assertNotIn("evil.example", unsafe_command)

    def test_published_article_review_timestamp_uses_friendly_format(self):
        article = {
            **self.articles[0],
            "review": {
                "status": "approved",
                "reviewed_by": "Knowledge Reviewer",
                "reviewed_at": "2026-08-27T20:15:00+00:00",
            },
        }
        with (
            patch("app.app.knowledge_repository.resolve_published_article", return_value=article),
            patch("app.app.relationship_service.related_commands_for_article", return_value=[]),
        ):
            html = self.client.get("/knowledge/published/dns-basics").get_data(as_text=True)

        self.assertIn("Aug 27, 2026", html)
        self.assertNotIn("2026-08-27T20:15:00+00:00", html)

    def test_no_match_and_empty_inventory_are_distinct_for_both_libraries(self):
        with patch("app.app.knowledge_repository.get_published", return_value=self.articles):
            filtered_articles = self.client.get("/knowledge/published?q=missing").get_data(as_text=True)
        self.assertIn("No articles match these filters", filtered_articles)
        self.assertIn("Clear filters", filtered_articles)
        self.assertNotIn("No published articles found", filtered_articles)

        with patch("app.app.knowledge_repository.get_published", return_value=[]):
            empty_articles = self.client.get("/knowledge/published").get_data(as_text=True)
        self.assertIn("No published articles found", empty_articles)
        self.assertNotIn("No articles match these filters", empty_articles)

        with patch("app.app.command_repository.get_all", return_value=self.commands):
            filtered_commands = self.client.get("/commands?category=Missing").get_data(as_text=True)
        self.assertIn("No commands match these filters", filtered_commands)
        self.assertIn("Clear filters", filtered_commands)

        with patch("app.app.command_repository.get_all", return_value=[]):
            empty_commands = self.client.get("/commands").get_data(as_text=True)
        self.assertIn("No commands found", empty_commands)
        self.assertNotIn("No commands match these filters", empty_commands)

    def test_manage_article_explicitly_enters_integrity_and_preserves_safe_inventory_return(self):
        policy = {
            "article": {"id": "dns-basics", "title": "DNS Basics"},
            "state": "published", "references": [], "aliases": [],
            "can_archive": False, "archive_reasons": ["Referenced"],
            "can_soft_delete": False, "soft_delete_reasons": ["Published"],
            "can_permanent_delete": False, "permanent_delete_reasons": ["Published"],
        }
        with patch(
            "app.app.KnowledgeIntegrityService.lifecycle_policy", return_value=policy,
        ):
            response = self.client.get(
                "/knowledge/manage/dns-basics?return_to="
                "%2Fknowledge%2Fpublished%3Fq%3Ddns%26category%3DNetworking"
            )
            unsafe = self.client.get(
                "/knowledge/manage/dns-basics?return_to=https%3A%2F%2Fevil.example"
            )
        html = response.get_data(as_text=True)
        self.assertIn("You are managing this published article in Knowledge Integrity", html)
        self.assertIn("Integrity dashboard", html)
        self.assertIn("Return to Published Articles", html)
        self.assertIn('href="/knowledge/published?q=dns&amp;category=Networking"', html)
        self.assertIn('aria-label="Return to Published Articles"', html)
        unsafe_html = unsafe.get_data(as_text=True)
        self.assertIn('href="/knowledge/published"', unsafe_html)
        self.assertNotIn("evil.example", unsafe_html)


if __name__ == "__main__":
    unittest.main()
