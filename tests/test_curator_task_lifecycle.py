import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_session_reconciliation_service import CuratorSessionReconciliationService
from app.services.curator_task_service import CuratorTaskService
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.models import AuditFilter, Finding
from curator.tasks import KnowledgeTaskService


class CuratorTaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"] = {"GKT-TEST": {
            "task_id": "GKT-TEST", "finding_id": "finding-1", "status": "open",
            "owner": "Human", "priority": "Medium", "classification": "Risk",
            "finding_type": "missing_safety_guidance", "title": "Review restart guidance",
            "content_type": "workflow_node", "content_identifier": "flow:restart",
            "curator_rule": "CUR-SAFE-L2", "explanation": "Review proportional guidance.",
            "recommended_action": "Review the current instruction.", "confidence": "high",
            "knowledge_debt_score": 11, "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00", "times_observed": 7,
            "related_content": ["flow:restart"], "related_workflows": ["flow"],
            "related_articles": [], "related_commands": [], "related_scripts": [],
            "evidence": ["Please perform a full system reboot of your computer."],
            "history": [{"event": "observed", "at": "2026-01-01T00:00:00+00:00",
                         "evidence": ["Please perform a full system reboot of your computer."]}],
            "resolution_history": [],
        }}
        self.store.save(state)
        self.service = CuratorTaskService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def finding(identifier="finding-1", evidence="Original evidence"):
        return Finding(identifier=identifier, classification="risk", finding_type="missing_safety_guidance",
                       severity="medium", confidence="high", content_type="workflow_node",
                       content_identifier="flow:restart", title="Review restart guidance",
                       explanation="Review proportional guidance.", evidence=[evidence],
                       rule="CUR-SAFE-L2", recommended_action="Review it.", domain="workflow",
                       safety_level=2)

    def test_resolve_accepts_current_note_and_records_session_metadata(self):
        task = self.service.update("GKT-TEST", action="resolve", note="Saved work guidance verified.",
                                   session_id="CFX-000000000001")
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(task["resolution_notes"], "Saved work guidance verified.")
        self.assertEqual(task["resolution_metadata"]["maintenance_session_id"], "CFX-000000000001")

    def test_saved_work_note_can_be_used_by_later_resolve_request(self):
        self.service.update("GKT-TEST", action="note", note="Reviewed current node against level 2 guidance.")
        task = self.service.update("GKT-TEST", action="resolve", note="")
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(task["resolution_notes"], "Reviewed current node against level 2 guidance.")

    def test_resolve_without_any_note_is_rejected_atomically(self):
        before = self.store.load()["tasks"]["GKT-TEST"]
        with self.assertRaisesRegex(CuratorMemoryError, "resolution note"):
            self.service.update("GKT-TEST", action="resolve")
        after = self.store.load()["tasks"]["GKT-TEST"]
        self.assertEqual(before, after)

    def test_invalid_transition_is_rejected_without_history_event(self):
        self.service.update("GKT-TEST", action="ignore", note="Not applicable.")
        before = self.store.load()["tasks"]["GKT-TEST"]
        with self.assertRaisesRegex(CuratorMemoryError, "cannot move"):
            self.service.update("GKT-TEST", action="start")
        self.assertEqual(before, self.store.load()["tasks"]["GKT-TEST"])

    def test_double_resolve_does_not_duplicate_decision_or_resolution_event(self):
        self.service.update("GKT-TEST", action="resolve", note="Verified.")
        state = self.store.load()
        decisions = len(state["decisions"])
        history = len(state["tasks"]["GKT-TEST"]["history"])
        self.store.update_task("GKT-TEST", status="resolved", event_name="resolve", note="Verified.")
        state = self.store.load()
        self.assertEqual(len(state["decisions"]), decisions)
        self.assertEqual(len(state["tasks"]["GKT-TEST"]["history"]), history)

    def test_reopen_preserves_resolution_history_and_allows_genuine_recurrence(self):
        self.service.update("GKT-TEST", action="resolve", note="Verified.")
        resolved_history = len(self.store.load()["tasks"]["GKT-TEST"]["resolution_history"])
        task = self.service.update("GKT-TEST", action="reopen", note="Finding recurred.")
        self.assertEqual(task["status"], "open")
        self.assertGreater(len(task["resolution_history"]), resolved_history)
        self.assertNotIn("resolved_at", task)

    def test_original_evidence_is_immutable_while_current_content_is_displayed(self):
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        (drafts / "flow.json").write_text(json.dumps({"workflow_id": "flow", "name": "Flow",
            "nodes": {"restart": {"title": "Restart", "instruction": "Save work, then restart Windows."}}}),
            encoding="utf-8")
        task = self.service.get("GKT-TEST")
        self.assertEqual(task["original_evidence"], ["Please perform a full system reboot of your computer."])
        self.assertEqual(task["current_content"]["instruction"], "Save work, then restart Windows.")

    def test_missing_current_workflow_content_fails_safe_without_changing_evidence(self):
        task = self.service.get("GKT-TEST")
        self.assertIsNone(task["current_content"])
        self.assertEqual(task["original_evidence"][0], "Please perform a full system reboot of your computer.")

    def test_targeted_verification_does_not_resolve_or_rewrite_original_evidence(self):
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        (drafts / "flow.json").write_text(json.dumps({"workflow_id": "flow", "name": "Flow",
            "nodes": {"restart": {"type": "instruction", "title": "Restart",
                                    "instruction": "Save work, then restart Windows."}}}), encoding="utf-8")
        before = self.store.load()["tasks"]["GKT-TEST"]
        result = CuratorTargetedVerificationService(self.root).verify("GKT-TEST")
        after = self.store.load()["tasks"]["GKT-TEST"]
        self.assertIn(result["status"], {"still_detected", "appears_corrected"})
        self.assertEqual(after["status"], "open")
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertTrue(after["current_verification"]["affected_fingerprint"])

    def test_unknown_rule_requires_human_review_instead_of_claiming_correction(self):
        state = self.store.load()
        state["tasks"]["GKT-TEST"]["curator_rule"] = "CUSTOM-HUMAN-RULE"
        self.store.save(state)
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        (drafts / "flow.json").write_text(json.dumps({"workflow_id": "flow", "name": "Flow",
            "nodes": {"restart": {"type": "instruction", "title": "Restart"}}}), encoding="utf-8")
        result = CuratorTargetedVerificationService(self.root).verify("GKT-TEST")
        self.assertEqual(result["status"], "human_review_required")

    def test_stale_fingerprint_blocks_resolution_atomically(self):
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        path = drafts / "flow.json"
        path.write_text(json.dumps({"workflow_id": "flow", "name": "Flow",
            "nodes": {"restart": {"type": "instruction", "title": "Restart"}}}), encoding="utf-8")
        fingerprint = self.service.get("GKT-TEST")["affected_fingerprint"]
        workflow = json.loads(path.read_text(encoding="utf-8"))
        workflow["nodes"]["restart"]["instruction"] = "Changed after page load."
        path.write_text(json.dumps(workflow), encoding="utf-8")
        with self.assertRaisesRegex(CuratorMemoryError, "changed after this page"):
            self.service.update("GKT-TEST", action="resolve", note="Verified.",
                                expected_fingerprint=fingerprint)
        self.assertEqual(self.store.load()["tasks"]["GKT-TEST"]["status"], "open")

    def test_repeated_audits_dedupe_by_rule_content_and_node_even_when_finding_id_changes(self):
        state = self.store.load()
        state["tasks"] = {}
        tasks = KnowledgeTaskService()
        first = tasks.reconcile(state, [self.finding("hash-one", "Old wording")], [], run_id="RUN-1",
                                observed_at="2026-01-01T00:00:00+00:00", filters=AuditFilter())
        second = tasks.reconcile(state, [self.finding("hash-two", "New wording")], [], run_id="RUN-2",
                                 observed_at="2026-01-02T00:00:00+00:00", filters=AuditFilter())
        self.assertEqual(len(state["tasks"]), 1)
        task = next(iter(state["tasks"].values()))
        self.assertEqual(task["times_observed"], 2)
        self.assertEqual(task["evidence"], ["Old wording"])
        self.assertEqual(task["current_evidence"], ["New wording"])
        self.assertEqual(first["observed"], second["observed"])

    def test_resolved_task_reopens_on_true_recurrence_without_creating_duplicate(self):
        state = {"tasks": {}}
        tasks = KnowledgeTaskService()
        tasks.reconcile(state, [self.finding()], [], run_id="RUN-1", observed_at="2026-01-01T00:00:00+00:00",
                        filters=AuditFilter())
        task = next(iter(state["tasks"].values()))
        task["status"] = "resolved"
        tasks.reconcile(state, [self.finding("changed-hash")], [], run_id="RUN-2",
                        observed_at="2026-01-02T00:00:00+00:00", filters=AuditFilter())
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["times_returned"], 1)

    def test_task_route_preserves_return_context_and_surfaces_safe_validation_message(self):
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorTaskService", return_value=self.service):
            with flask_app.test_client() as client:
                response = client.post("/curator/tasks/GKT-TEST/actions", data={
                    "action": "resolve", "origin": "maintenance",
                    "return_to": "/curator/fix/CFX-000000000001"},
                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add a resolution note", response.data)
        self.assertIn(b"Return to Maintenance", response.data)

    def test_successful_route_resolution_triggers_session_reconciliation_once(self):
        reconciler = Mock()
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorTaskService", return_value=self.service), \
             patch("app.app.CuratorSessionReconciliationService", return_value=reconciler):
            with flask_app.test_client() as client:
                response = client.post("/curator/tasks/GKT-TEST/actions", data={
                    "action": "resolve", "note": "Verified proportional warning.",
                    "curator_session": "CFX-000000000001",
                    "return_to": "/curator/fix/CFX-000000000001"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        reconciler.reconcile.assert_called_once_with("CFX-000000000001", trigger="task_resolution")

    def test_reconciliation_attributes_explicit_task_resolution_to_session_not_external(self):
        item = {"item_id": "FIX-1", "status": "open", "knowledge_debt": 10,
                "affected_content": {"task_id": "GKT-TEST"}}
        sessions = CuratorFixSessionService(self.root)
        created = sessions.create(started_by="Reviewer", originating_audit_id="AUD-1", queue=[item],
                                  baseline={"counts": {"broken_relationships": 1}})
        self.service.update("GKT-TEST", action="resolve", note="Verified.", session_id=created["session_id"])
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value={"counts": {"broken_relationships": 0}})
        reconciler.planner.build = Mock(return_value=[])
        reconciler.tasks.reconcile_external = Mock()
        result = reconciler.reconcile(created["session_id"], trigger="task_resolution")
        self.assertEqual(result["repair_queue"][0]["status"], "completed")
        self.assertEqual(result["session_debt_reduced"], 10)
        self.assertEqual(result["external_debt_reduced"], 0)
        reconciler.tasks.reconcile_external.assert_not_called()

    def test_reconciliation_and_debt_are_idempotent_on_refresh(self):
        item = {"item_id": "FIX-1", "status": "open", "knowledge_debt": 10,
                "affected_content": {"task_id": "GKT-TEST"}}
        sessions = CuratorFixSessionService(self.root)
        created = sessions.create(started_by="Reviewer", originating_audit_id=None, queue=[item],
                                  baseline={"counts": {"broken_relationships": 1}})
        self.service.update("GKT-TEST", action="resolve", note="Verified.", session_id=created["session_id"])
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value={"counts": {"broken_relationships": 0}})
        reconciler.planner.build = Mock(return_value=[])
        first = reconciler.reconcile(created["session_id"], trigger="task_resolution")
        second = reconciler.reconcile(created["session_id"], trigger="refresh")
        self.assertEqual(first["debt_reduced"], second["debt_reduced"])
        completed = second["outcomes"]["completed"]
        self.assertEqual(sum(entry["item_id"] == "FIX-1" for entry in completed), 1)


if __name__ == "__main__":
    unittest.main()
