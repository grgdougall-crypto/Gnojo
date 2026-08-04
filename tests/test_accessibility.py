import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.app import app


class InteractiveParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = []
        self.dialogs = []
        self.images = []
        self._button = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "button":
            self._button = {"attrs": values, "text": ""}
            self.buttons.append(self._button)
        elif tag == "dialog":
            self.dialogs.append(values)
        elif tag == "img":
            self.images.append(values)

    def handle_endtag(self, tag):
        if tag == "button":
            self._button = None

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def parse(self, route):
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200, route)
        parser = InteractiveParser()
        parser.feed(response.get_data(as_text=True))
        return response.get_data(as_text=True), parser

    def test_shared_layout_has_skip_link_landmark_and_live_region(self):
        html, _ = self.parse("/")
        self.assertIn('class="skip-link" href="#mainContent"', html)
        self.assertIn('<main id="mainContent" tabindex="-1">', html)
        self.assertIn('id="a11yStatus"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('aria-label="Primary navigation"', html)
        self.assertIn("accessibility.js", html)

    def test_major_pages_have_named_buttons_images_and_dialogs(self):
        for route in ("/", "/device-profiles", "/workflow-editor/vpn_connectivity_win.json", "/wizard?workflow=internet", "/search?q=VPN"):
            with self.subTest(route=route):
                _, parser = self.parse(route)
                for button in parser.buttons:
                    self.assertTrue(button["text"].strip() or button["attrs"].get("aria-label") or button["attrs"].get("title"), button)
                for image in parser.images:
                    self.assertIn("alt", image)
                for dialog in parser.dialogs:
                    self.assertTrue(dialog.get("aria-labelledby") or dialog.get("aria-label"), dialog)

    def test_dynamic_statuses_are_marked_for_announcements(self):
        html, _ = self.parse("/workflow-editor/vpn_connectivity_win.json")
        for target in ("nodeDetailsHint", "validationSummary", "publicationMessage", "settingsMessage", "aiSuggestionMessage", "nodeSearchEmpty"):
            pattern = rf'id="{target}"[^>]*data-a11y-live|data-a11y-live[^>]*id="{target}"'
            self.assertRegex(html, pattern)

    def test_focus_and_reduced_motion_styles_exist(self):
        css = Path("app/static/css/style.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn(".skip-link:focus", css)


if __name__ == "__main__":
    unittest.main()
