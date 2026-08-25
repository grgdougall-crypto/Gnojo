import unittest
from copy import deepcopy
from pathlib import Path

from app.services.curator_verification_presentation_service import CuratorVerificationPresentationService


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


class CuratorUxPassOneTests(unittest.TestCase):
    def test_shared_navigation_has_all_destinations_and_task_anchor(self):
        nav = (TEMPLATES / "partials" / "_curator_nav.html").read_text(encoding="utf-8")
        for label in ["Overview", "Knowledge Tasks", "Integrity", "Maintenance: Fix Wizard", "Growth"]:
            self.assertIn(label, nav)
        self.assertIn("#knowledge-tasks", nav)
        self.assertIn('id="knowledge-tasks"', (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8"))

    def test_major_curator_surfaces_include_shared_navigation_and_active_state(self):
        expected = {
            "curator_dashboard.html": "overview",
            "curator_task_detail.html": "tasks",
            "knowledge_integrity.html": "integrity",
            "curator_fix_start.html": "maintenance",
            "curator_fix_wizard.html": "maintenance",
            "curator_fix_complete.html": "maintenance",
            "curator_growth.html": "growth",
        }
        for filename, active in expected.items():
            source = (TEMPLATES / filename).read_text(encoding="utf-8")
            self.assertIn("partials/_curator_nav.html", source)
            self.assertIn("curator_active='{}'".format(active), source)
        task = (TEMPLATES / "curator_task_detail.html").read_text(encoding="utf-8")
        self.assertIn("task_navigation.return_label", task)

    def test_dashboard_lifecycle_explainer_has_five_steps_and_audit_controls(self):
        source = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        for step in ["1. Audit", "2. Findings", "3. Knowledge Tasks", "4. Verify", "5. Supervised Maintenance"]:
            self.assertIn(step, source)
        self.assertIn("Growth is separate governance", source)
        lifecycle_end = source.index("</ol>")
        self.assertGreater(source.index("Growth is separate governance"), lifecycle_end)
        self.assertIn("Run Curator Audit", source)
        self.assertIn("Assisted Resolution", source)

    def test_content_studio_groups_each_existing_destination_once(self):
        source = (TEMPLATES / "content_studio.html").read_text(encoding="utf-8")
        self.assertIn("Measure and Govern", source)
        self.assertIn("Create and Manage", source)
        expected_order = [
            "Create and Manage",
            "url_for('workflow_studio')",
            "url_for('article_builder')",
            "url_for('command_builder')",
            "url_for('script_builder')",
            "Measure and Govern",
            "url_for('curator_dashboard')",
            "url_for('content_quality')",
        ]
        positions = [source.index(value) for value in expected_order]
        self.assertEqual(positions, sorted(positions))
        for endpoint in ["curator_dashboard", "content_quality", "workflow_studio", "article_builder", "command_builder", "script_builder"]:
            self.assertEqual(source.count("url_for('{}')".format(endpoint)), 1)

    def test_every_known_verification_state_has_stable_non_mutating_presentation(self):
        states = ["relationship_satisfied", "relationship_missing", "relationship_conflict_or_unresolved", "target_unavailable", "still_detected", "appears_corrected", "human_review_required"]
        for state in states:
            verification = {"status": state, "message": "technical detail"}
            before = deepcopy(verification)
            result = CuratorVerificationPresentationService.present(verification)
            self.assertEqual(verification, before)
            self.assertEqual(result["technical_state"], state)
            self.assertTrue(result["headline"])
            self.assertTrue(result["explanation"])
            self.assertTrue(result["next_action"])

    def test_maintenance_language_and_completion_actions_are_clear(self):
        wizard = (TEMPLATES / "curator_fix_wizard.html").read_text(encoding="utf-8")
        for label in ["Ready to address now", "Repairs applied in this session", "Resolved outside this maintenance session", "Remaining review items", "Deferred for later", "Current knowledge debt"]:
            self.assertIn(label, wizard)
        complete = (TEMPLATES / "curator_fix_complete.html").read_text(encoding="utf-8")
        for action in ["Run Full Curator Audit", "View Remaining Work", "Return to Curator Dashboard", "Start Another Fix Wizard Session"]:
            self.assertIn(action, complete)

    def test_fix_wizard_entry_and_session_labels_use_one_capability_name(self):
        dashboard = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        integrity = (TEMPLATES / "knowledge_integrity.html").read_text(encoding="utf-8")
        start = (TEMPLATES / "curator_fix_start.html").read_text(encoding="utf-8")
        wizard = (TEMPLATES / "curator_fix_wizard.html").read_text(encoding="utf-8")

        self.assertIn("Open Fix Wizard", dashboard)
        self.assertIn("Open Fix Wizard", integrity)
        for label in ["<h1>Fix Wizard</h1>", "Start Fix Wizard Session",
                      "Resume Fix Wizard Session", "Resume a Fix Wizard Session"]:
            self.assertIn(label, start)
        for label in ["<h1>Fix Wizard</h1>", "Fix Wizard session navigation",
                      "Leave Fix Wizard", "Finish Fix Wizard Session"]:
            self.assertIn(label, wizard)

        for obsolete in ["Start Fix Wizard</a>", "Resume Maintenance Session",
                         "Create Maintenance Session"]:
            self.assertNotIn(obsolete, dashboard + integrity + start)


if __name__ == "__main__":
    unittest.main()
