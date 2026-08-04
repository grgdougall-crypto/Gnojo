import unittest
from pathlib import Path

from app.app import app


class GnojoBrandingTests(unittest.TestCase):
    def test_primary_pages_render_gnojo_brand(self):
        with app.test_client() as client:
            for route in ("/", "/content-studio", "/workflow-studio"):
                response = client.get(route)
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200, route)
                self.assertIn("Gnojo", html, route)
                self.assertNotIn("SupportPilot", html, route)
                self.assertNotIn("Mission Control", html, route)

    def test_theme_key_migrates_to_gnojo_without_losing_preference(self):
        script = Path("app/static/js/theme.js").read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("gnojo-theme")', script)
        self.assertIn('localStorage.getItem("supportpilot-theme")', script)
        self.assertIn('localStorage.setItem("gnojo-theme", newTheme)', script)


if __name__ == "__main__":
    unittest.main()
