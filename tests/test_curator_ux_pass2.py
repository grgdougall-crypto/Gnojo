import unittest
from pathlib import Path

from app.services.curator_dashboard_presentation_service import CuratorDashboardPresentationService


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


class CuratorUxPassTwoTests(unittest.TestCase):
    def test_working_set_preserves_order_counts_and_complete_inventory(self):
        tasks = [{"task_id": f"GKT-{index:02d}"} for index in range(9)]

        def group(items):
            return [{"title": "Existing order", "tasks": list(items)}] if items else []

        result = CuratorDashboardPresentationService.present(tasks, group_tasks=group)
        self.assertEqual(result["total_count"], 9)
        self.assertEqual(result["displayed_count"], 6)
        self.assertEqual(result["remaining_count"], 3)
        self.assertEqual([item["task_id"] for item in result["working_groups"][0]["tasks"]], [f"GKT-{index:02d}" for index in range(6)])
        self.assertEqual([item["task_id"] for item in result["remaining_groups"][0]["tasks"]], ["GKT-06", "GKT-07", "GKT-08"])
        self.assertEqual(len(tasks), 9)

    def test_dashboard_has_correct_lifecycle_and_separate_growth_governance(self):
        source = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        expected = ["1. Audit", "2. Findings", "3. Knowledge Tasks", "4. Verify", "5. Supervised Maintenance"]
        positions = [source.index(label) for label in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("5. Growth", source)
        self.assertGreater(source.index("Growth is separate governance"), source.index("</ol>"))

    def test_dashboard_exposes_prioritized_set_and_complete_inventory(self):
        source = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        for text in ["Prioritized Knowledge Tasks", "Showing {{ work.displayed_count }} of {{ work.total_count }}", "View complete task inventory", "remaining_groups"]:
            self.assertIn(text, source)
        self.assertIn("<details", source)
        self.assertIn("<summary", source)
        for sort_value in ["debt", "priority", "recurrence", "category", "platform", "owner", "status", "age", "confidence"]:
            self.assertIn("'{}'".format(sort_value), source)

    def test_fix_wizard_groups_navigation_item_and_session_actions(self):
        source = (TEMPLATES / "curator_fix_wizard.html").read_text(encoding="utf-8")
        for label in ["Session navigation", "Actions for this finding", "Session actions", "Entire Fix Wizard session"]:
            self.assertIn(label, source)
        for action in ['value="deferred"', 'value="skipped"', 'value="rejected"', "Open Curator Task", "Repair All Safe Items", "Finish Fix Wizard Session"]:
            self.assertIn(action, source)
        self.assertLess(source.index("Actions for this finding"), source.index("Repair All Safe Items"))

    def test_task_detail_keeps_fix_wizard_return_context(self):
        source = (TEMPLATES / "curator_task_detail.html").read_text(encoding="utf-8")
        self.assertIn("task_navigation.return_label", source)
        self.assertIn('name="origin"', source)
        self.assertIn("return_to", source)


if __name__ == "__main__":
    unittest.main()
