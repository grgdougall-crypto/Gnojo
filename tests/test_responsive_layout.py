import tempfile
import unittest
from pathlib import Path

from app.app import app
from tests.ui_fixtures import write_ui_workflow


class ResponsiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_ui_workflow(self.root)
        self.previous_testing = app.config.get("TESTING")
        self.previous_workflow_root = app.config.get("WORKFLOW_REPOSITORY_ROOT")
        app.config.update(
            TESTING=True,
            WORKFLOW_REPOSITORY_ROOT=str(self.root),
        )
        self.client = app.test_client()

    def tearDown(self):
        app.config["TESTING"] = self.previous_testing
        if self.previous_workflow_root is None:
            app.config.pop("WORKFLOW_REPOSITORY_ROOT", None)
        else:
            app.config["WORKFLOW_REPOSITORY_ROOT"] = self.previous_workflow_root
        self.temporary.cleanup()

    def test_major_pages_render_for_responsive_browser_checks(self):
        routes = (
            "/",
            "/device-profiles",
            "/workflow-editor/vpn_connectivity_win.json",
            "/wizard?workflow=internet",
            "/search?q=VPN",
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_workflow_editor_has_mobile_panel_navigation(self):
        html = self.client.get(
            "/workflow-editor/vpn_connectivity_win.json"
        ).get_data(as_text=True)
        self.assertIn('class="workflow-mobile-tabs"', html)
        for panel in ("summary", "browser", "details"):
            self.assertIn(f'data-workflow-panel="{panel}"', html)
            self.assertIn(f'data-workflow-panel-name="{panel}"', html)

    def test_responsive_breakpoints_and_touch_targets_are_defined(self):
        shared_css = Path("app/static/css/style.css").read_text(encoding="utf-8")
        designer_css = Path("app/static/css/workflow_designer.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("@media (max-width: 767.98px)", shared_css)
        self.assertIn("@media (max-width: 575.98px)", shared_css)
        self.assertIn("@media (pointer: coarse)", shared_css)
        self.assertIn("100dvh", shared_css)
        self.assertIn(".workflow-mobile-tabs", designer_css)
        self.assertIn(".workflow-panel.is-mobile-active", designer_css)
        self.assertIn("100dvh", designer_css)


if __name__ == "__main__":
    unittest.main()
