import unittest
from unittest.mock import patch

from app.app import app


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
        self.assertIn('aria-label="View article: DNS Basics"', html)
        self.assertIn('aria-label="Manage article: DNS Basics"', html)

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
        self.assertIn('aria-label="View command: Inspect IP Configuration"', html)

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


if __name__ == "__main__":
    unittest.main()
