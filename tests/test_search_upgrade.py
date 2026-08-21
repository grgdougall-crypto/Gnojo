import unittest
from unittest.mock import Mock, patch

from app.app import app
from app.services.search_service import SearchService


class SearchUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.service = SearchService()
        self.service.knowledge = Mock()
        self.service.commands = Mock()
        self.service.knowledge.get_published.return_value = [{
            "id": "dns-guide", "title": "DNS Troubleshooting", "overview": "Resolve name lookup failures.",
            "category": "Networking", "difficulty": "Beginner", "tags": ["dns", "network"],
        }]
        self.service.commands.get_all.return_value = [{
            "id": "ipconfig", "name": "ipconfig", "title": "Inspect IP configuration",
            "summary": "View DHCP, DNS, and adapter details.", "category": "Networking",
            "shell": "Command Prompt", "difficulty": "Beginner", "platforms": ["Windows"], "tags": ["dhcp"],
        }]
        self.snapshot = {
            "publication": {"version": 2},
            "workflow": {
                "workflow_id": "vpn_help", "name": "VPN Connection Help",
                "description": "Diagnose VPN connectivity.", "start_node": "start",
                "nodes": {"start": {"type": "question", "title": "VPN adapter", "question": "Can the VPN connect?"}},
            },
        }

    def search(self, query):
        with patch("app.services.search_service.WorkflowPublicationService") as publications:
            publications.return_value.list_current.return_value = [self.snapshot]
            return self.service.search_all(query)

    def test_multi_term_query_matches_individual_terms_and_ranks_titles(self):
        results = self.search("Wi-Fi DNS DHCP VPN routing")
        self.assertEqual({result.content_type for result in results}, {"Article", "Command", "Workflow"})
        exact = self.search("VPN Connection Help")
        self.assertEqual(exact[0].content_type, "Workflow")

    def test_search_page_filters_workflows_and_links_to_wizard(self):
        results = self.search("VPN")
        with patch("app.app.search_service.search_all", return_value=results):
            response = app.test_client().get("/search?q=VPN&type=workflow")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Start Workflow", html)
        self.assertIn("/wizard?workflow=vpn_help", html)
        self.assertNotIn("View Article", html)

    def test_initial_state_does_not_search_or_claim_no_results(self):
        with patch("app.app.search_service.search_all") as search_all:
            response = app.test_client().get("/search")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        search_all.assert_not_called()
        self.assertIn("What are you looking for?", html)
        self.assertNotIn("No results found", html)
        self.assertIn("Browse knowledge", html)
        self.assertIn("Browse commands", html)
        self.assertIn("Browse workflows", html)

    def test_empty_results_offer_real_browse_destinations(self):
        with patch("app.app.search_service.search_all", return_value=[]):
            response = app.test_client().get("/search?q=unlikelyterm")
        html = response.get_data(as_text=True)
        self.assertIn("No results found", html)
        self.assertIn('value="unlikelyterm"', html)
        self.assertIn("Try a shorter phrase", html)
        self.assertIn("Browse workflows", html)
        self.assertNotIn('href="#"', html)


if __name__ == "__main__":
    unittest.main()
