import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.app import app
from curator.memory import CuratorMemoryStore
from tests.ui_fixtures import write_ui_workflow


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
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_ui_workflow(self.root)
        (self.root / "curation_memory").mkdir(parents=True, exist_ok=True)
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = {"GKT-A11Y": {
            "task_id": "GKT-A11Y", "finding_id": "CUR-A11Y",
            "status": "open", "owner": "Human", "priority": "Medium",
            "classification": "Risk", "finding_type": "ui_fixture",
            "title": "Accessibility fixture", "content_type": "workflow",
            "content_identifier": "vpn_connectivity_win",
            "curator_rule": "CUR-UI-FIXTURE",
            "explanation": "Exercise the task-detail landmark.",
            "recommended_action": "Inspect the rendered task.",
            "confidence": "high", "knowledge_debt_score": 1,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00", "times_observed": 1,
            "related_content": [], "related_workflows": [],
            "related_articles": [], "related_commands": [], "related_scripts": [],
            "evidence": ["Fixture evidence."], "history": [],
            "resolution_history": [],
        }}
        store.save(state)
        self.previous_testing = app.config.get("TESTING")
        self.previous_workflow_root = app.config.get("WORKFLOW_REPOSITORY_ROOT")
        self.previous_structural_root = app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        app.config.update(
            TESTING=True,
            WORKFLOW_REPOSITORY_ROOT=str(self.root),
            STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root),
        )
        self.client = app.test_client()

    def tearDown(self):
        app.config["TESTING"] = self.previous_testing
        for key, value in (
            ("WORKFLOW_REPOSITORY_ROOT", self.previous_workflow_root),
            ("STRUCTURAL_REPAIR_REPOSITORY_ROOT", self.previous_structural_root),
        ):
            if value is None:
                app.config.pop(key, None)
            else:
                app.config[key] = value
        self.temporary.cleanup()

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

        _, task_parser = self.parse("/curator/tasks/GKT-A11Y")
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
