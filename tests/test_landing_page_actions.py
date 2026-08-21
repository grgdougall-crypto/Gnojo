import unittest

from app.app import app


class LandingPageActionTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.response = self.client.get("/")
        self.html = self.response.get_data(as_text=True)

    def test_landing_page_has_no_dead_placeholder_links(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertNotIn('href="#"', self.html)
        self.assertIn('href="#workflows"', self.html)
        self.assertIn("Create Device Profile", self.html)
        self.assertIn('/device-profiles', self.html)

    def test_primary_destinations_load(self):
        for route in ("/content-studio", "/workflow-studio", "/knowledge", "/device-profiles"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_knowledge_center_exposes_existing_command_library_with_other_destinations(self):
        response = self.client.get("/knowledge")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Command Library", html)
        self.assertIn("Browse Commands", html)
        self.assertIn('href="/commands"', html)
        self.assertEqual(self.client.get("/commands").status_code, 200)
        for destination in (
            "Search Gnojo Knowledge",
            "Knowledge Studio",
            "Review Drafts",
            "Browse Published Articles",
            "Learning Library",
        ):
            with self.subTest(destination=destination):
                self.assertIn(destination, html)

    def test_service_areas_link_to_focused_searches(self):
        self.assertIn("Windows+macOS+applications+printers+user+access", self.html)
        self.assertIn("Wi-Fi+DNS+DHCP+VPN+routing+network+diagnostics", self.html)
        self.assertIn("Windows+Server+Active+Directory+Group+Policy+permissions", self.html)
        self.assertIn("security+alerts+logs+packet+captures+escalation", self.html)


if __name__ == "__main__":
    unittest.main()
