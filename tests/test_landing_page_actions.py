import unittest

from app.app import app


class LandingPageActionTests(unittest.TestCase):
    def setUp(self):
        self.previous_testing = app.config.get("TESTING")
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.response = self.client.get("/")
        self.html = self.response.get_data(as_text=True)

    def tearDown(self):
        app.config["TESTING"] = self.previous_testing

    def test_landing_page_has_no_dead_placeholder_links(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertNotIn('href="#"', self.html)
        self.assertIn('href="#workflows"', self.html)
        self.assertIn("Create Device Profile", self.html)
        self.assertIn('/device-profiles', self.html)
        self.assertIn("Knowledge Center", self.html)
        self.assertNotIn(">Knowledge Base<", self.html)

    def test_primary_destinations_load(self):
        for route in ("/content-studio", "/workflow-studio", "/knowledge", "/device-profiles"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_knowledge_center_exposes_v1_knowledge_destinations(self):
        response = self.client.get("/knowledge")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Published Articles", html)
        self.assertIn("Commands", html)
        self.assertIn("Scripts", html)
        self.assertIn("Search", html)
        self.assertIn("Browse Commands", html)
        self.assertIn('href="/commands"', html)
        self.assertIn('href="/scripts"', html)
        self.assertNotIn("Review Drafts", html)
        self.assertNotIn("Command Builder", html)
        self.assertEqual(self.client.get("/commands").status_code, 200)

    def test_authenticated_product_shell_uses_four_area_navigation(self):
        for label in ("Troubleshoot", "Knowledge", "Build", "Health"):
            self.assertIn(f">\n                    {label}\n", self.html)
        self.assertIn('href="/content-studio"', self.html)
        self.assertIn('href="/curator"', self.html)
        header = self.html[self.html.index('<header class="app-header">'):self.html.index("</header>")]
        self.assertNotIn(">Review<", header)
        self.assertNotIn("Content Studio", header)

    def test_troubleshoot_home_exposes_required_shortcuts(self):
        for label in ("Guided Troubleshooting", "Browse Workflows", "History", "Device Profiles"):
            self.assertIn(label, self.html)

    def test_command_builder_identifies_itself_truthfully(self):
        html = self.client.get("/commands/builder").get_data(as_text=True)
        self.assertIn("<title>\nCommand Builder | Gnojo", html)
        self.assertIn("Content Studio", html)
        self.assertIn("Command Builder", html)
        self.assertNotIn("Knowledge Studio", html)

    def test_service_areas_link_to_focused_searches(self):
        self.assertIn("Windows+macOS+applications+printers+user+access", self.html)
        self.assertIn("Wi-Fi+DNS+DHCP+VPN+routing+network+diagnostics", self.html)
        self.assertIn("Windows+Server+Active+Directory+Group+Policy+permissions", self.html)
        self.assertIn("security+alerts+logs+packet+captures+escalation", self.html)


if __name__ == "__main__":
    unittest.main()
