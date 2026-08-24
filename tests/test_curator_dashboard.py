import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import render_template

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
            "task_presentation": {
                "total_count": 1,
                "displayed_count": 1,
                "remaining_count": 0,
                "working_groups": [{"title": "Critical Today", "tasks": [{
                    "task_id": "GKT-TEST", "classification": "Defect", "priority": "High",
                    "status": "open", "execution_mode": "human_review", "title": "Reference observation",
                    "recommended_action": "Review the reference.", "owner": "QA Reviewer",
                    "knowledge_debt_score": 12, "times_observed": 1,
                }]}],
                "remaining_groups": [],
            },
            "task_inventory": {
                "filters": {"q": "", "status": "", "include_resolved": "", "classification": "", "workflow": "", "family": "", "rule": "", "disposition": ""},
                "visible": 1, "total": 1, "active": False, "show_calibration": False,
                "closed_count": 0,
                "calibration": {},
                "options": {
                    "statuses": ["open", "resolved"],
                    "classifications": ["Defect", "Risk", "Opportunity", "Recommendation"],
                    "workflows": [("windows_slow", "Computer Running Slowly")],
                    "rules": [
                        ("CUR-WR-EARLY-CONVERGENCE", "Early Branch Convergence"),
                        ("CUR-WR-SIGNAL-RETENTION", "Strong Signal Not Preserved"),
                        ("CUR-WR-ACTION-VERIFICATION", "Action Without Verification"),
                        ("CUR-WR-TERMINAL-EVIDENCE", "Terminal Claim Exceeds Evidence"),
                        ("CUR-WR-PROGRESS", "Progress Inconsistency"),
                    ],
                    "dispositions": [
                        ("NOT_REVIEWED", "Not Reviewed"), ("USEFUL", "Useful"),
                        ("INTENTIONAL", "Intentional"), ("FALSE_POSITIVE", "False Positive"),
                    ],
                },
            },
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
        self.assertIn(
            'href="/curator/tasks/GKT-TEST?origin=knowledge_tasks&amp;'
            'return_to=/curator%23knowledge-tasks"', html,
        )

    @patch("app.app.CuratorDashboardService")
    def test_run_button_executes_audit_and_redirects(self, service):
        service.return_value.run_audit.return_value = {"run_id": "RUN-1"}
        response = self.client.post("/curator/run")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/curator?status=completed")
        service.return_value.run_audit.assert_called_once_with()

    @patch("app.app.CuratorBatchService")
    @patch("app.app.CuratorDashboardService")
    def test_prepared_batch_entries_link_to_existing_task_package_section(
            self, dashboard_service, batch_service):
        dashboard_service.return_value.dashboard.return_value = self.dashboard
        batch_service.return_value.latest.return_value = {
            "at": "2026-08-24T12:00:00+00:00",
            "prepared": [
                {"task_id": "GKT-PREPARED", "recommendation": "CREATE_NEW_ARTICLE", "version": 2},
            ],
            "failed": [{"task_id": "GKT-FAILED", "error": "private diagnostic"}],
        }

        html = self.client.get("/curator").get_data(as_text=True)

        self.assertIn('id="assisted-resolution-batch"', html)
        self.assertIn("Review package for GKT-PREPARED", html)
        self.assertIn(
            '/curator/tasks/GKT-PREPARED?origin=assisted_resolution_batch&amp;'
            'return_to=/curator%23assisted-resolution-batch#assisted-resolution', html,
        )
        self.assertIn("Packages not prepared", html)
        self.assertIn("GKT-FAILED", html)
        self.assertNotIn("private diagnostic", html)
        self.assertNotIn("Review package for GKT-FAILED", html)

    @patch("app.app.CuratorBatchService")
    @patch("app.app.CuratorDashboardService")
    def test_dashboard_keeps_empty_batch_state_unchanged(self, dashboard_service, batch_service):
        dashboard_service.return_value.dashboard.return_value = self.dashboard
        batch_service.return_value.latest.return_value = {}
        html = self.client.get("/curator").get_data(as_text=True)
        self.assertIn("Prepare First Assisted Resolution Batch", html)
        self.assertNotIn("Prepared packages", html)
        self.assertNotIn("Packages not prepared", html)

    @patch("app.app.CuratorBatchService")
    def test_batch_preparation_still_redirects_to_dashboard(self, batch_service):
        response = self.client.post("/curator/assisted-resolution/first-batch")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/curator?status=batch_completed")
        batch_service.return_value.prepare_first_batch.assert_called_once_with()

    @patch("app.app.CuratorTaskService")
    def test_reasoning_disposition_route_records_calibration(self, service):
        response = self.client.post(
            "/curator/tasks/GKT-WR/review-disposition",
            data={"disposition": "USEFUL", "return_to": "/curator?family=workflow_reasoning#knowledge-tasks"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("notice=disposition_updated", response.headers["Location"])
        self.assertTrue(response.headers["Location"].endswith("#knowledge-tasks"))
        service.return_value.update_review_disposition.assert_called_once_with("GKT-WR", "USEFUL")

    @patch("app.app.CuratorDashboardService")
    def test_status_query_remains_an_inventory_filter(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        self.client.get("/curator?status=open")
        self.assertEqual(service.return_value.dashboard.call_args.kwargs["filters"]["status"], "open")

    @patch("app.app.CuratorDashboardService")
    def test_default_queue_is_actionable_and_resolved_can_be_included(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        default = self.client.get("/curator").get_data(as_text=True)
        self.assertIn('<option value="">Actionable</option>', default)
        self.assertIn("Include resolved", default)
        self.assertEqual(
            service.return_value.dashboard.call_args.kwargs["filters"]["include_resolved"], ""
        )

        self.client.get("/curator?include_resolved=1")
        self.assertEqual(
            service.return_value.dashboard.call_args.kwargs["filters"]["include_resolved"], "1"
        )

    def test_resolved_task_renders_only_reopen_while_open_task_restores_controls(self):
        base = {
            "task_id": "GKT-STATE", "title": "State test", "explanation": "Review state.",
            "classification": "Risk", "priority": "Medium", "owner": "Unassigned",
            "knowledge_debt_score": 1, "confidence": "high", "navigation": {"url": "/curator", "label": "Open affected content"},
            "guidance": {"why": "Review.", "impact": "Low.", "certainty": "Human review required."},
            "recommended_action": "Review.", "original_evidence": [], "current_content": None,
            "history": [], "related_workflows": [], "related_articles": [], "related_commands": [],
            "related_scripts": [], "related_tasks": [], "live_related_knowledge": {"articles": []},
            "finding_type": "missing_safety_guidance", "future_automated_fix": False,
            "affected_fingerprint": "fingerprint", "current_verification": None,
        }
        context = {
            "owners": ["Unassigned"], "priorities": ["Medium"], "status_kind": "info",
            "status_message": "", "resolution_package": None, "confusing_step_proposal": None,
            "verification_presentation": None,
            "task_review": {"history_count": 0, "recent_history": [], "remaining_history": []},
            "session_task_actionable": False, "return_to": "", "curator_session": "", "category": "all",
        }
        with app.test_request_context():
            resolved = render_template("curator_task_detail.html", task={**base, "status": "resolved"}, **context)
            opened = render_template("curator_task_detail.html", task={**base, "status": "open"}, **context)
        self.assertIn("Reopen task", resolved)
        for label in ("Assign owner", "Set priority", "Mark in progress", "Defer", "Ignore", "Add note"):
            self.assertNotIn(label, resolved)
            self.assertIn(label, opened)

    @patch("app.app.CuratorDashboardService")
    def test_inventory_dropdowns_render_actual_options(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        html = self.client.get("/curator").get_data(as_text=True)
        expected_options = (
            'value="open"', 'value="Defect"',
            'value="windows_slow">Computer Running Slowly</option>',
            'value="workflow_reasoning">Workflow Reasoning</option>',
            'value="CUR-WR-EARLY-CONVERGENCE">Early Branch Convergence</option>',
            'value="CUR-WR-SIGNAL-RETENTION">Strong Signal Not Preserved</option>',
            'value="CUR-WR-ACTION-VERIFICATION">Action Without Verification</option>',
            'value="CUR-WR-TERMINAL-EVIDENCE">Terminal Claim Exceeds Evidence</option>',
            'value="CUR-WR-PROGRESS">Progress Inconsistency</option>',
            'value="NOT_REVIEWED">Not Reviewed</option>',
            'value="USEFUL">Useful</option>',
            'value="INTENTIONAL">Intentional</option>',
            'value="FALSE_POSITIVE">False Positive</option>',
        )
        for option in expected_options:
            self.assertIn(option, html)

    @patch("app.app.CuratorDashboardService")
    def test_inventory_dropdowns_preserve_selected_values(self, service):
        selected = self.dashboard["task_inventory"]["filters"]
        selected.update({
            "workflow": "windows_slow",
            "family": "workflow_reasoning",
            "rule": "CUR-WR-SIGNAL-RETENTION",
            "disposition": "NOT_REVIEWED",
        })
        service.return_value.dashboard.return_value = self.dashboard
        html = self.client.get(
            "/curator?workflow=windows_slow&family=workflow_reasoning"
            "&rule=CUR-WR-SIGNAL-RETENTION&disposition=NOT_REVIEWED"
        ).get_data(as_text=True)
        self.assertIn('value="windows_slow" selected>Computer Running Slowly</option>', html)
        self.assertIn('value="workflow_reasoning" selected>Workflow Reasoning</option>', html)
        self.assertIn('value="CUR-WR-SIGNAL-RETENTION" selected>Strong Signal Not Preserved</option>', html)
        self.assertIn('value="NOT_REVIEWED" selected>Not Reviewed</option>', html)

    @patch("app.app.CuratorDashboardService")
    def test_inventory_clear_link_resets_to_unfiltered_dashboard(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        html = self.client.get("/curator?workflow=windows_slow").get_data(as_text=True)
        self.assertIn('href="/curator#knowledge-tasks">Clear</a>', html)

    @patch("app.app.CuratorDashboardService")
    def test_inventory_dropdown_options_are_not_duplicated(self, service):
        service.return_value.dashboard.return_value = self.dashboard
        html = self.client.get("/curator").get_data(as_text=True)
        self.assertEqual(html.count('value="windows_slow"'), 1)
        for rule, _ in self.dashboard["task_inventory"]["options"]["rules"]:
            self.assertEqual(html.count(f'value="{rule}"'), 1)


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
