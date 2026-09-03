import re
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from app.app import app


class InteractiveParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = []
        self.dialogs = []
        self.images = []
        self.ids = []
        self.labels_for = []
        self.main_count = 0
        self._button = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "main":
            self.main_count += 1
        elif tag == "label" and values.get("for"):
            self.labels_for.append(values["for"])
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

    def test_base_owns_the_only_main_landmark(self):
        child_templates = [
            path
            for path in Path("app/templates").rglob("*.html")
            if path.name != "base.html"
        ]
        for template in child_templates:
            with self.subTest(template=str(template)):
                self.assertNotRegex(template.read_text(encoding="utf-8"), r"<main\b")

        for route in (
            "/",
            "/content-studio",
            "/workflow-editor/vpn_connectivity_win.json",
            "/curator",
            "/curator/fix",
            "/curator/growth",
            "/knowledge",
            "/knowledge/builder",
            "/commands/builder",
            "/scripts/builder",
            "/troubleshooting-history",
        ):
            with self.subTest(route=route):
                _, parser = self.parse(route)
                self.assertEqual(parser.main_count, 1)

        curator_html, _ = self.parse("/curator")
        task_link = re.search(r'href="([^\"]*/curator/tasks/[^\"]+)"', curator_html)
        self.assertIsNotNone(task_link)
        _, task_parser = self.parse(unescape(task_link.group(1)))
        self.assertEqual(task_parser.main_count, 1)

    def test_scoped_active_controls_have_programmatic_names(self):
        workflow_html, _ = self.parse("/workflow-editor/vpn_connectivity_win.json")
        for expected in (
            'id="simulatorPlatform" aria-label="Simulation platform"',
            'id="simulatorDeviceType" aria-label="Simulation device type"',
            'id="simulatorConnection" aria-label="Simulation connection type"',
            'id="helpTextPreviewValue" class="node-editor-control" rows="6" readonly aria-label="Generated help text preview"',
        ):
            self.assertIn(expected, workflow_html)

        script_html, _ = self.parse("/scripts/builder")
        self.assertIn('id="scriptSourceHeading"', script_html)
        self.assertIn('id="scriptSourceInput" name="source"', script_html)
        self.assertIn('aria-labelledby="scriptSourceHeading"', script_html)

        knowledge_html, _ = self.parse("/knowledge")
        self.assertIn(
            '<label class="visually-hidden" for="knowledgeLibrarySearch">Search published knowledge</label>',
            knowledge_html,
        )
        self.assertIn('id="knowledgeLibrarySearch"', knowledge_html)

        growth_html, growth_parser = self.parse("/curator/growth")
        for prefix in (
            "proposalDecision",
            "proposalReviewer",
            "proposalReason",
            "lessonDecision",
            "lessonReviewer",
            "lessonReason",
        ):
            matching_ids = [value for value in growth_parser.ids if value.startswith(prefix)]
            for control_id in matching_ids:
                self.assertIn(control_id, growth_parser.labels_for)

    def test_changed_pages_do_not_introduce_duplicate_ids(self):
        for route in (
            "/workflow-editor/vpn_connectivity_win.json",
            "/knowledge",
            "/scripts/builder",
            "/curator/growth",
        ):
            with self.subTest(route=route):
                _, parser = self.parse(route)
                self.assertEqual(len(parser.ids), len(set(parser.ids)))

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
        for target in ("nodeDetailsHint", "validationSummary", "publicationMessage", "workflowPublicationSuccess", "settingsMessage", "aiSuggestionMessage", "nodeSearchEmpty"):
            pattern = rf'id="{target}"[^>]*data-a11y-live|data-a11y-live[^>]*id="{target}"'
            self.assertRegex(html, pattern)

    def test_focus_and_reduced_motion_styles_exist(self):
        css = Path("app/static/css/style.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn(".skip-link:focus", css)


if __name__ == "__main__":
    unittest.main()
