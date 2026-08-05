import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_task_service import CuratorTaskService
from curator.memory import CuratorMemoryError, CuratorMemoryStore


class CuratorDashboardServiceTests(unittest.TestCase):
    def test_dashboard_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = CuratorDashboardService(Path(directory)).dashboard()
        self.assertFalse(dashboard["has_audit"])
        self.assertEqual(dashboard["tasks"], [])


class CuratorDashboardPageTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.dashboard = {
            "has_audit": True,
            "latest": {
                "summary": {"findings": 4, "findings_by_classification": {"defect": 1}},
                "knowledge_tasks": {"summary": {"open": 3, "in_progress": 1}},
                "knowledge_debt": {"total": 22, "trend": "stable"},
                "knowledge_health": {"overall_score": 91, "trend": "improving", "dimensions": {"content_quality": 90}},
                "lessons_learned": {"lessons": []},
            },
            "tasks": [{
                "task_id": "GKT-TEST", "classification": "Defect", "priority": "High",
                "status": "open", "title": "Reference observation", "recommended_action": "Review the reference.",
                "owner": "QA Reviewer", "knowledge_debt_score": 12, "times_observed": 1,
            }],
            "task_groups": [{"title": "Critical Today", "tasks": [{
                "task_id": "GKT-TEST", "classification": "Defect", "priority": "High",
                "status": "open", "title": "Reference observation", "recommended_action": "Review the reference.",
                "owner": "QA Reviewer", "knowledge_debt_score": 12, "times_observed": 1,
            }]}],
            "curator_status": {"current_state": "Idle", "last_audit": "2026-08-05T12:00:00+00:00", "audit_duration": 1.25, "curator_version": "2.0.0", "memory_size": 2, "active_tasks": 1, "resolved_tasks": 0, "debt_trend": "stable"},
            "evolution": [{"at": "2026-08-05T12:00:00+00:00", "event": "Audit completed", "detail": "4 findings."}],
            "sort_by": "debt",
            "recent_audits": [{"run_id": "RUN-1", "completed_at": "2026-08-05T12:00:00+00:00", "summary": {"findings": 4}}],
        }

    def test_content_studio_links_to_curator(self):
        html = self.client.get("/content-studio").get_data(as_text=True)
        self.assertIn('href="/curator"', html)
        self.assertIn("Curator Dashboard", html)

    @patch("app.app.CuratorDashboardService")
    def test_dashboard_renders_operational_information(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        response = self.client.get("/curator")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Run Curator Audit", html)
        self.assertIn("Knowledge Tasks", html)
        self.assertIn("GKT-TEST", html)
        self.assertIn("Operational Health", html)
        self.assertIn("Curator Status", html)
        self.assertIn("Knowledge Evolution", html)
        self.assertIn('href="/curator/tasks/GKT-TEST"', html)

    @patch("app.app.CuratorDashboardService")
    def test_run_button_executes_audit_and_redirects(self, service):
        service.return_value.run_audit.return_value = {"run_id": "RUN-1"}
        response = self.client.post("/curator/run")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/curator?status=completed")
        service.return_value.run_audit.assert_called_once_with()


class CuratorKnowledgeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"]["GKT-TEST"] = {
            "task_id": "GKT-TEST", "finding_id": "finding-1", "classification": "Defect",
            "finding_type": "broken_reference", "content_type": "article", "content_identifier": "missing-article",
            "title": "Broken article reference", "status": "open", "priority": "High", "owner": "QA Reviewer",
            "knowledge_debt_score": 20, "confidence": "high", "first_seen": "2026-08-05T12:00:00+00:00",
            "last_seen": "2026-08-05T12:00:00+00:00", "times_observed": 1,
            "recommended_action": "Repair the reference.", "evidence": ["Article missing-article was not found."],
            "history": [], "resolution_history": [], "related_content": ["missing-article"],
        }
        self.store.save(state)
        self.service = CuratorTaskService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_task_detail_enriches_navigation_and_guidance(self):
        task = self.service.get("GKT-TEST")
        self.assertEqual(task["navigation"]["label"], "Open affected article")
        self.assertIn("/knowledge/published/missing-article", task["navigation"]["url"])
        self.assertTrue(task["guidance"]["human_required"])

    def test_task_actions_are_persisted_in_history(self):
        self.service.update("GKT-TEST", action="start", note="Review started.")
        self.service.update("GKT-TEST", action="priority", priority="Critical")
        task = self.service.get("GKT-TEST")
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["priority"], "Critical")
        self.assertEqual(len(task["history"]), 2)

    def test_resolution_requires_a_note(self):
        with self.assertRaises(CuratorMemoryError):
            self.service.update("GKT-TEST", action="resolve")


if __name__ == "__main__":
    unittest.main()
