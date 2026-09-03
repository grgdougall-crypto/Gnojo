import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.structural_repair_fixtures import pre_stage34_network_diagnostics_bytes

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.curator_fix_session_service import CuratorFixSessionError, CuratorFixSessionService
from app.services.curator_repair_executor import CuratorRepairError, CuratorRepairExecutor
from app.services.curator_repair_planner import CuratorRepairPlanner
from app.services.curator_session_reconciliation_service import CuratorSessionReconciliationService
from app.services.curator_task_reconciliation_service import CuratorTaskReconciliationService
from app.services.curator_task_service import CuratorTaskService
from curator.memory import CuratorMemoryStore
from app.app import app as flask_app


class CuratorFixWizardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def baseline(**overrides):
        counts = {"broken_relationships": 1, "duplicate_groups": 0, "inventory_mismatches": 0,
                  "missing_review_metadata": 1, "orphaned_articles": 1}
        counts.update(overrides)
        return {"counts": counts}

    @staticmethod
    def empty_integrity_report():
        return {
            "broken_relationships": [], "duplicate_groups": [],
            "inventory_mismatches": [], "orphaned_articles": [],
            "missing_review_metadata": [], "counts": {},
        }

    def structural_task(self, **overrides):
        task = {
            "task_id": "GKT-STRUCTURAL", "finding_id": "CUR-STRUCTURAL",
            "status": "open", "classification": "Risk",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
            "priority": "Medium", "confidence": "high", "knowledge_debt_score": 11,
            "title": "Terminal diagnosis may exceed collected evidence",
            "recommended_action": "Review the governed structural repair.",
            "structured_evidence": {
                "requirement": "dns_resolution", "terminal": "dns_problem",
                "missing": ["external_ip_reachability"], "affected_path_count": 1,
                "affected_paths": [{
                    "nodes": ["inspect_ip_configuration", "check_ip_address", "test_gateway",
                              "gateway_result", "test_dns", "dns_result", "dns_problem"],
                    "missing": ["external_ip_reachability"],
                    "predecessor_edge": {
                        "source": "dns_result", "route": "No", "destination": "dns_problem",
                    },
                }],
                "predecessor_edges": [{
                    "source": "dns_result", "route": "No", "destination": "dns_problem",
                }],
            },
        }
        task.update(overrides)
        return task

    def install_structural_draft(self):
        destination = self.root / "app/workflow_drafts/network_diagnostics.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(pre_stage34_network_diagnostics_bytes())
        return destination

    @staticmethod
    def safe_relink(source="app/workflow_drafts/test.json"):
        return {"item_id": "FIX-000000000001", "finding_type": "broken_relationship",
                "classification": "RELINK_EXISTING", "safe_automatic": True, "reversible": True,
                "what_will_change": "Reference changes.", "what_will_not_change": "Content remains unchanged.",
                "affected_content": {"workflow": "test", "node": "step", "source": source,
                                     "current_reference": "Canonical Article",
                                     "canonical_target": "canonical-article", "before": "Canonical Article",
                                     "after": "canonical-article"}}

    def test_session_creation_persistence_resume_and_debt(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(started_by="Reviewer", originating_audit_id="AUD-1",
                                 queue=[{"item_id": "FIX-1", "status": "open"}],
                                 baseline=self.baseline())
        resumed = service.get(session["session_id"])
        self.assertEqual(resumed["started_by"], "Reviewer")
        self.assertEqual(resumed["originating_audit_id"], "AUD-1")
        self.assertEqual(resumed["finding_count"], 1)
        self.assertEqual(resumed["starting_debt"], 13)
        summaries = service.list_sessions()
        self.assertEqual(summaries[0]["session_id"], session["session_id"])
        self.assertEqual(summaries[0]["handled"], 0)

    def test_create_or_resume_prevents_duplicate_active_sessions(self):
        service = CuratorFixSessionService(self.root)
        first, resumed = service.create_or_resume(
            started_by="Reviewer", originating_audit_id="AUD-1",
            queue=[{"item_id": "FIX-1", "status": "open"}], baseline=self.baseline())
        second, resumed_second = service.create_or_resume(
            started_by=" reviewer ", originating_audit_id="AUD-2",
            queue=[{"item_id": "FIX-2", "status": "open"}], baseline=self.baseline())
        self.assertFalse(resumed)
        self.assertTrue(resumed_second)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(service.list_sessions()), 1)

    def test_corrupt_session_is_ignored_by_resume_listing(self):
        service = CuratorFixSessionService(self.root)
        service.directory.mkdir(parents=True)
        (service.directory / "CFX-000000000001.json").write_text("{not-json", encoding="utf-8")
        self.assertEqual(service.list_sessions(), [])

    def test_failed_serialization_leaves_no_partial_session(self):
        service = CuratorFixSessionService(self.root)
        with self.assertRaises(CuratorFixSessionError):
            service.create(started_by="Reviewer", originating_audit_id=None,
                           queue=[], baseline={"counts": self.baseline()["counts"], "bad": {object()}})
        self.assertEqual(list(service.directory.glob("CFX-*.json")), [])

    def test_real_fix_wizard_post_redirect_get_round_trip_and_refresh(self):
        service = CuratorFixSessionService(self.root)
        baseline = self.baseline()
        reconciler = Mock()
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.KnowledgeIntegrityService.report", return_value=baseline), \
             patch("app.app.CuratorRepairPlanner.build", return_value=[]), \
             patch("app.app.CuratorSessionReconciliationService", return_value=reconciler), \
             patch("app.app.CuratorMemoryStore.load", return_value={"audits": [{"run_id": "AUD-RUNTIME"}]}):
            with flask_app.test_client() as client:
                self.assertEqual(client.get("/curator/fix").status_code, 200)
                response = client.post("/curator/fix", data={"reviewer": "Browser Reviewer"})
                self.assertEqual(response.status_code, 302)
                target = response.headers["Location"]
                session_path = service.directory / f"{service.list_sessions()[0]['session_id']}.json"
                before_get = session_path.read_bytes()
                page = client.get(target)
                self.assertEqual(page.status_code, 200)
                self.assertIn(b"Browser Reviewer", page.data)
                self.assertIn(b"Starting queue (baseline)", page.data)
                self.assertEqual(client.get(target).status_code, 200)
                self.assertEqual(session_path.read_bytes(), before_get)
                reconciler.reconcile.assert_not_called()
                duplicate = client.post("/curator/fix", data={"reviewer": "Browser Reviewer"})
                self.assertEqual(duplicate.status_code, 302)
                self.assertEqual(len(service.list_sessions()), 1)
        persisted = service.get(service.list_sessions()[0]["session_id"])
        self.assertEqual(persisted["started_by"], "Browser Reviewer")
        self.assertEqual(persisted["starting_integrity"], baseline)

    def test_resume_card_uses_dynamic_queue_counters_without_impossible_progress(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(
            started_by="Greg Dougall", originating_audit_id="AUD-DYNAMIC",
            queue=[
                {"item_id": "FIX-1", "status": "completed"},
                {"item_id": "FIX-2", "status": "resolved_external"},
            ],
            baseline=self.baseline(),
        )
        session["repair_queue"].extend([
            {"item_id": "FIX-3", "status": "deferred", "introduced_after_start": True},
            {"item_id": "FIX-4", "status": "open", "introduced_after_start": True},
            {"item_id": "FIX-5", "status": "completed", "introduced_after_start": True},
        ])
        service.save(session)
        path = service.directory / f"{session['session_id']}.json"
        before = path.read_bytes()
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.KnowledgeIntegrityService.report", return_value=self.baseline()):
            with flask_app.test_client() as client:
                response = client.get("/curator/fix")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Reviewer Greg Dougall", page)
        self.assertIn("2</strong> starting", page)
        self.assertIn("3</strong> added", page)
        self.assertIn("1</strong> remaining", page)
        self.assertIn("4 handled", page)
        self.assertIn("2 completed in this session", page)
        self.assertIn("1 resolved externally", page)
        self.assertNotIn("4 of 2 reviewed", page)
        self.assertNotIn(" of 2 reviewed", page)
        self.assertEqual(path.read_bytes(), before)

    def test_resume_card_handles_zero_added_and_zero_remaining_states(self):
        service = CuratorFixSessionService(self.root)
        no_added = service.create(
            started_by="No Added", originating_audit_id=None,
            queue=[{"item_id": "FIX-A", "status": "open"}], baseline=self.baseline(),
        )
        finished_queue = service.create(
            started_by="Queue Finished", originating_audit_id=None,
            queue=[{"item_id": "FIX-B", "status": "completed"}], baseline=self.baseline(),
        )
        finished_queue["repair_queue"].append(
            {"item_id": "FIX-C", "status": "resolved_external", "introduced_after_start": True}
        )
        service.save(finished_queue)
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.KnowledgeIntegrityService.report", return_value=self.baseline()):
            with flask_app.test_client() as client:
                page = client.get("/curator/fix").get_data(as_text=True)

        self.assertIn("1</strong> starting", page)
        self.assertIn("0</strong> added", page)
        self.assertIn("1</strong> remaining", page)
        self.assertIn("1</strong> added", page)
        self.assertIn("0</strong> remaining", page)
        self.assertIn(no_added["session_id"], page)
        self.assertIn(finished_queue["session_id"], page)

    def test_detail_and_completion_status_use_authoritative_dynamic_progress_read_only(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(
            started_by="Status Reviewer", originating_audit_id=None,
            queue=[
                {"item_id": "FIX-1", "status": "completed", "finding_type": "safety_risk"},
                {"item_id": "FIX-2", "status": "resolved_external", "finding_type": "safety_risk"},
            ],
            baseline=self.baseline(),
        )
        session["repair_queue"].extend([
            {"item_id": "FIX-3", "status": "deferred", "finding_type": "safety_risk",
             "introduced_after_start": True},
            {"item_id": "FIX-4", "status": "open", "finding_type": "editorial_opportunity",
             "classification": "MANUAL", "safe_automatic": False, "priority": "Medium",
             "knowledge_debt": 1, "confidence": 80, "estimated_effort": "Low",
             "recommended_action": "Review the finding.", "what_will_change": "Nothing yet.",
             "what_will_not_change": "Content remains unchanged.",
             "affected_content": {"task_id": "GKT-4", "content_identifier": "example",
                                  "evidence": "Example evidence."},
             "introduced_after_start": True},
        ])
        service.save(session)
        path = service.directory / f"{session['session_id']}.json"
        before = path.read_bytes()
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService") as session_service_class, \
             patch("app.app.CuratorSessionReconciliationService") as reconciler:
            session_service_class.return_value = service
            session_service_class.progress.side_effect = CuratorFixSessionService.progress
            with flask_app.test_client() as client:
                detail = client.get(f"/curator/fix/{session['session_id']}")
                complete = client.get(f"/curator/fix/{session['session_id']}/complete")

        for page in (detail.get_data(as_text=True), complete.get_data(as_text=True)):
            self.assertIn("Starting queue (baseline)", page)
            self.assertIn("Added during session", page)
            self.assertIn("Resolved externally", page)
            self.assertIn("Deferred for later", page)
            self.assertIn("Remaining open items", page)
            self.assertIn("Resolved externally</span><strong>1", page)
            self.assertIn("Deferred for later</span><strong>1", page)
            self.assertIn("Remaining open items</span><strong>1", page)
        self.assertIn("Items handled", complete.get_data(as_text=True))
        self.assertIn("Items handled</span><strong>3", complete.get_data(as_text=True))
        self.assertIn("Completed in this session</span><strong>1", complete.get_data(as_text=True))
        self.assertNotIn("Human review remains", complete.get_data(as_text=True))
        self.assertEqual(path.read_bytes(), before)
        reconciler.return_value.reconcile.assert_not_called()

    def test_finish_session_authority_and_completion_event_are_unchanged(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(
            started_by="Finishing Reviewer", originating_audit_id=None,
            queue=[{"item_id": "FIX-1", "status": "open"}], baseline=self.baseline(),
        )
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.KnowledgeIntegrityService.report", return_value=self.baseline()):
            with flask_app.test_client() as client:
                response = client.post(f"/curator/fix/{session['session_id']}/complete")

        self.assertEqual(response.status_code, 200)
        persisted = service.get(session["session_id"])
        self.assertIsNotNone(persisted["ended_at"])
        self.assertEqual(
            [event["event"] for event in persisted["events"]].count("session_completed"),
            1,
        )

    def test_fix_wizard_get_does_not_append_reconciliation_event(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(
            started_by="Read-only Reviewer", originating_audit_id="AUD-READ-ONLY",
            queue=[{"item_id": "FIX-READ-ONLY", "status": "open",
                    "finding_type": "editorial_opportunity", "classification": "HUMAN_REVIEW",
                    "affected_content": {"id": "example"}}],
            baseline=self.baseline(),
        )
        path = service.directory / f"{session['session_id']}.json"
        protected_paths = {
            "curator_memory": self.root / "curation_memory" / "memory.json",
            "workflow": self.root / "app" / "workflow_drafts" / "flow.json",
            "publication": self.root / "workflow_publications" / "flow" / "manifest.json",
            "stage_b": self.root / "curation_memory" / "stage_b_reconciliations" / "journal.jsonl",
        }
        for marker, protected_path in protected_paths.items():
            protected_path.parent.mkdir(parents=True, exist_ok=True)
            protected_path.write_bytes(f"{marker}-sentinel".encode("utf-8"))
        protected_before = {name: value.read_bytes() for name, value in protected_paths.items()}
        before = path.read_bytes()
        before_events = list(service.get(session["session_id"])["events"])
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.CuratorSessionReconciliationService") as reconciler:
            with flask_app.test_client() as client:
                for _ in range(3):
                    response = client.get(f"/curator/fix/{session['session_id']}")
                    self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    client.get(f"/curator/fix/{session['session_id']}/refresh").status_code,
                    405,
                )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            {name: value.read_bytes() for name, value in protected_paths.items()},
            protected_before,
        )
        self.assertEqual(service.get(session["session_id"])["events"], before_events)
        reconciler.return_value.reconcile.assert_not_called()

    def test_manual_refresh_redirect_reports_changed_and_unchanged_truthfully(self):
        flask_app.config.update(TESTING=True)
        reconciler = Mock()
        reconciler.reconcile.side_effect = [
            {"last_reconciliation": {"changed": True}},
            {"last_reconciliation": {"changed": False}},
        ]
        with patch("app.app.CuratorSessionReconciliationService", return_value=reconciler):
            with flask_app.test_client() as client:
                changed = client.post("/curator/fix/CFX-000000000001/refresh")
                unchanged = client.post("/curator/fix/CFX-000000000001/refresh")
        self.assertIn("status=reconciled_changed", changed.headers["Location"])
        self.assertIn("status=reconciled_unchanged", unchanged.headers["Location"])

    def test_empty_reviewer_and_persistence_failure_are_handled_without_500(self):
        service = CuratorFixSessionService(self.root)
        baseline = self.baseline()
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorFixSessionService", return_value=service), \
             patch("app.app.KnowledgeIntegrityService.report", return_value=baseline), \
             patch("app.app.CuratorRepairPlanner.build", return_value=[]), \
             patch("app.app.CuratorMemoryStore.load", return_value={"audits": []}):
            with flask_app.test_client() as client:
                empty = client.post("/curator/fix", data={"reviewer": ""})
                self.assertEqual(empty.status_code, 400)
                with patch.object(service, "create_or_resume", side_effect=OSError("private path")):
                    failed = client.post("/curator/fix", data={"reviewer": "Reviewer"})
                self.assertEqual(failed.status_code, 503)
                self.assertNotIn(b"private path", failed.data)
        self.assertEqual(list(service.directory.glob("CFX-*.json")), [])

    def test_invalid_session_ids_are_rejected(self):
        with self.assertRaises(CuratorFixSessionError):
            CuratorFixSessionService(self.root).get("../memory")

    def test_outcome_does_not_reduce_unverified_debt(self):
        service = CuratorFixSessionService(self.root)
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[{"item_id": "FIX-1", "status": "open"}], baseline=self.baseline())
        updated = service.record(session["session_id"], "FIX-1", "deferred")
        self.assertEqual(updated["repair_queue"][0]["status"], "deferred")
        self.assertEqual(updated["debt_reduced"], 0)

    def test_task_deferral_maps_to_exactly_one_open_session_item(self):
        service = CuratorFixSessionService(self.root)
        item = {"item_id": "FIX-TASK", "status": "open", "finding_type": "editorial_opportunity",
                "affected_content": {"task_id": "GKT-DEFER"}}
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[item], baseline=self.baseline())

        updated = service.record_task_outcome(session["session_id"], "GKT-DEFER", "deferred")

        self.assertEqual(updated["repair_queue"][0]["status"], "deferred")
        self.assertEqual(updated["outcomes"]["deferred"][0]["item_id"], "FIX-TASK")

    def test_task_deferral_is_persistent_and_updates_all_queue_counters(self):
        service = CuratorFixSessionService(self.root)
        queue = [
            {"item_id": "FIX-ONE", "status": "open", "finding_type": "editorial_opportunity",
             "affected_content": {"task_id": "GKT-ONE"}},
            {"item_id": "FIX-TWO", "status": "open", "finding_type": "editorial_opportunity",
             "affected_content": {"task_id": "GKT-TWO"}},
        ]
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=queue, baseline=self.baseline())
        service.record_task_outcome(session["session_id"], "GKT-ONE", "deferred")

        persisted = service.get(session["session_id"])
        progress = service.progress(persisted)

        self.assertEqual(progress["deferred"], 1)
        self.assertEqual(progress["current_actionable"], 1)
        self.assertEqual(progress["remaining"], 1)
        self.assertEqual(progress["handled"], 1)
        self.assertEqual([item["item_id"] for item in persisted["repair_queue"]
                          if item["status"] == "open"], ["FIX-TWO"])

    def test_task_deferral_rejects_missing_or_ambiguous_session_mapping(self):
        service = CuratorFixSessionService(self.root)
        queue = [
            {"item_id": "FIX-ONE", "status": "open", "affected_content": {"task_id": "GKT-DUP"}},
            {"item_id": "FIX-TWO", "status": "open", "affected_content": {"task_id": "GKT-DUP"}},
        ]
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=queue, baseline=self.baseline())
        with self.assertRaises(CuratorFixSessionError):
            service.record_task_outcome(session["session_id"], "GKT-MISSING", "deferred")
        with self.assertRaises(CuratorFixSessionError):
            service.record_task_outcome(session["session_id"], "GKT-DUP", "deferred")
        self.assertEqual(service.progress(service.get(session["session_id"]))["deferred"], 0)

    def test_task_action_route_defers_task_and_returns_to_same_session(self):
        session_service = CuratorFixSessionService(self.root)
        item = {"item_id": "FIX-TASK", "status": "open", "finding_type": "editorial_opportunity",
                "affected_content": {"task_id": "GKT-DEFER"}}
        session = session_service.create(started_by="Reviewer", originating_audit_id=None,
                                         queue=[item], baseline=self.baseline())
        task_store = CuratorMemoryStore(self.root / "curation_memory")
        state = task_store.load()
        state["tasks"]["GKT-DEFER"] = {
            "task_id": "GKT-DEFER", "status": "open", "owner": "Curator",
            "priority": "Medium", "history": [],
            "classification": "Opportunity", "finding_type": "article_candidate",
            "content_identifier": "workflow:step", "title": "Article opportunity",
        }
        task_store.save(state)
        task_service = CuratorTaskService(self.root)
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.baseline())
        reconciler.planner.build = Mock(return_value=[item])
        return_to = f"/curator/fix/{session['session_id']}"
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorTaskService", return_value=task_service), \
             patch("app.app.CuratorFixSessionService", return_value=session_service), \
             patch("app.app.CuratorSessionReconciliationService", return_value=reconciler):
            with flask_app.test_client() as client:
                response = client.post("/curator/tasks/GKT-DEFER/actions", data={
                    "action": "defer", "curator_session": session["session_id"],
                    "origin": "maintenance",
                    "return_to": return_to,
                })

        self.assertEqual(response.status_code, 302)
        self.assertIn(return_to, response.headers["Location"])
        self.assertEqual(task_store.load()["tasks"]["GKT-DEFER"]["status"], "deferred")
        queue_item = session_service.get(session["session_id"])["repair_queue"][0]
        self.assertEqual(queue_item["status"], "deferred")
        self.assertEqual(queue_item["external_task_state"]["status"], "deferred")

    def test_ineligible_session_task_does_not_mutate_task_on_defer(self):
        session_service = CuratorFixSessionService(self.root)
        session = session_service.create(started_by="Reviewer", originating_audit_id=None,
                                         queue=[], baseline=self.baseline())
        task_store = CuratorMemoryStore(self.root / "curation_memory")
        state = task_store.load()
        state["tasks"]["GKT-NO-DEFER"] = {
            "task_id": "GKT-NO-DEFER", "status": "open", "owner": "Curator",
            "priority": "Medium", "history": [],
            "classification": "Opportunity", "finding_type": "article_candidate",
            "content_identifier": "workflow:step", "title": "Article opportunity",
        }
        task_store.save(state)
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorTaskService", return_value=CuratorTaskService(self.root)), \
             patch("app.app.CuratorFixSessionService", return_value=session_service):
            with flask_app.test_client() as client:
                response = client.post("/curator/tasks/GKT-NO-DEFER/actions", data={
                    "action": "defer", "curator_session": session["session_id"],
                    "return_to": f"/curator/fix/{session['session_id']}",
                })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(task_store.load()["tasks"]["GKT-NO-DEFER"]["status"], "open")

    def test_deferred_item_stays_deferred_during_unchanged_reconciliation(self):
        service = CuratorFixSessionService(self.root)
        item = self.safe_relink()
        item["affected_content"]["task_id"] = "GKT-DEFER"
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[item], baseline=self.baseline())
        service.record_task_outcome(session["session_id"], "GKT-DEFER", "deferred")
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.baseline())
        reconciler.planner.build = Mock(return_value=[item])

        first = reconciler.reconcile(session["session_id"], trigger="test")
        second = reconciler.reconcile(session["session_id"], trigger="test")

        self.assertEqual(first["repair_queue"][0]["status"], "deferred")
        self.assertEqual(second["repair_queue"][0]["status"], "deferred")
        self.assertEqual(len(second["outcomes"]["deferred"]), 1)

    def test_external_task_deferral_reconciles_queue_advances_and_survives_resume(self):
        service = CuratorFixSessionService(self.root)
        first = {"item_id": "FIX-A", "status": "open", "finding_type": "editorial_opportunity",
                 "affected_content": {"task_id": "GKT-A"}}
        second = {"item_id": "FIX-B", "status": "open", "finding_type": "editorial_opportunity",
                  "affected_content": {"task_id": "GKT-B"}}
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[first, second], baseline=self.baseline())
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = {
            "GKT-A": {"task_id": "GKT-A", "status": "deferred", "history": []},
            "GKT-B": {"task_id": "GKT-B", "status": "open", "history": []},
        }
        store.save(state)
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.baseline())
        reconciler.planner.build = Mock(return_value=[first, second])

        updated = reconciler.reconcile(session["session_id"], trigger="manual_refresh")
        progress = service.progress(updated)

        self.assertEqual(updated["repair_queue"][0]["status"], "deferred")
        self.assertTrue(updated["repair_queue"][0]["external_task_state"])
        self.assertEqual(progress["deferred"], 1)
        self.assertEqual(progress["handled"], 1)
        self.assertEqual(progress["remaining"], 1)
        self.assertEqual(progress["current_actionable"], 1)
        self.assertEqual([item["item_id"] for item in updated["repair_queue"]
                          if item["status"] == "open"], ["FIX-B"])
        self.assertTrue(updated["last_reconciliation"]["changed"])

        resumed = reconciler.reconcile(session["session_id"], trigger="resume")
        repeated = reconciler.reconcile(session["session_id"], trigger="manual_refresh")
        self.assertEqual(service.progress(resumed), service.progress(repeated))
        self.assertEqual(len(repeated["outcomes"]["deferred"]), 1)
        self.assertFalse(repeated["last_reconciliation"]["changed"])
        self.assertEqual([item["item_id"] for item in repeated["repair_queue"]
                          if item["status"] == "open"], ["FIX-B"])
        summary = service.list_sessions()[0]
        self.assertEqual(summary["handled"], 1)
        self.assertEqual(summary["progress"]["remaining"], 1)

    def test_authoritative_non_actionable_task_states_map_to_precise_session_outcomes(self):
        cases = (
            ("deferred", {}, "deferred"),
            ("ignored", {}, "rejected"),
            ("resolved", {}, "resolved_external"),
            ("resolved", {"maintenance_session_id": "CURRENT"}, "completed"),
            ("superseded", {}, "unavailable_external"),
        )
        for index, (task_status, metadata, expected) in enumerate(cases):
            with self.subTest(task_status=task_status, metadata=metadata):
                service = CuratorFixSessionService(self.root)
                task_id = f"GKT-{index}"
                item = {"item_id": f"FIX-{index}", "status": "open",
                        "finding_type": "editorial_opportunity",
                        "affected_content": {"task_id": task_id}}
                session = service.create(started_by=f"Reviewer-{index}", originating_audit_id=None,
                                         queue=[item], baseline=self.baseline())
                if metadata.get("maintenance_session_id") == "CURRENT":
                    metadata = {"maintenance_session_id": session["session_id"]}
                store = CuratorMemoryStore(self.root / "curation_memory")
                state = store.load()
                state.setdefault("tasks", {})[task_id] = {
                    "task_id": task_id, "status": task_status,
                    "resolution_metadata": metadata, "history": [],
                }
                store.save(state)
                reconciler = CuratorSessionReconciliationService(self.root)
                reconciler.integrity.report = Mock(return_value=self.baseline())
                reconciler.planner.build = Mock(return_value=[item])

                updated = reconciler.reconcile(session["session_id"], trigger="test")

                self.assertEqual(updated["repair_queue"][0]["status"], expected)
                self.assertEqual(len(updated["outcomes"][expected]), 1)
                self.assertEqual(updated["outcomes"][expected][0]["verification"]["source"],
                                 "knowledge_task")

    def test_missing_authoritative_task_becomes_unavailable_once(self):
        service = CuratorFixSessionService(self.root)
        item = {"item_id": "FIX-MISSING", "status": "open",
                "finding_type": "editorial_opportunity",
                "affected_content": {"task_id": "GKT-MISSING"}}
        session = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[item], baseline=self.baseline())
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.baseline())
        reconciler.planner.build = Mock(return_value=[item])

        first = reconciler.reconcile(session["session_id"], trigger="resume")
        second = reconciler.reconcile(session["session_id"], trigger="resume")

        self.assertEqual(first["repair_queue"][0]["status"], "unavailable_external")
        self.assertEqual(service.progress(second)["external_unavailable"], 1)
        self.assertEqual(service.progress(second)["handled"], 1)
        self.assertEqual(len(second["outcomes"]["unavailable_external"]), 1)
        self.assertFalse(second["last_reconciliation"]["changed"])

    def test_reconciliation_reclassifies_open_item_without_replacing_session(self):
        service = CuratorFixSessionService(self.root)
        original = self.safe_relink()
        original.update({"classification": "CREATE_ARTICLE_REQUIRED", "safe_automatic": False})
        created = service.create(started_by="Reviewer", originating_audit_id="AUD-1",
                                 queue=[original], baseline=self.baseline())
        current = self.baseline(broken_relationships=1, orphaned_articles=0)
        latest = self.safe_relink()
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=current)
        reconciler.planner.build = Mock(return_value=[latest])
        reconciler.tasks.reconcile_classification = Mock(return_value=[])
        reconciled = reconciler.reconcile(created["session_id"], trigger="test")
        item = reconciled["repair_queue"][0]
        self.assertEqual(reconciled["session_id"], created["session_id"])
        self.assertEqual(item["classification"], "RELINK_EXISTING")
        self.assertEqual(item["previous_classification"], "CREATE_ARTICLE_REQUIRED")
        self.assertEqual(item["original_snapshot"]["classification"], "CREATE_ARTICLE_REQUIRED")
        self.assertEqual(item["latest_snapshot"]["classification"], "RELINK_EXISTING")

    def test_reconciliation_marks_disappeared_finding_resolved_externally(self):
        service = CuratorFixSessionService(self.root)
        created = service.create(started_by="Reviewer", originating_audit_id="AUD-1",
                                 queue=[self.safe_relink()], baseline=self.baseline())
        current = self.baseline(broken_relationships=0, orphaned_articles=0,
                                missing_review_metadata=0)
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=current)
        reconciler.planner.build = Mock(return_value=[])
        reconciler.tasks.reconcile_external = Mock(return_value=[])
        reconciled = reconciler.reconcile(created["session_id"], trigger="test")
        self.assertEqual(reconciled["repair_queue"][0]["status"], "resolved_external")
        self.assertEqual(reconciled["reconciliation_summary"]["external_resolutions"], 1)
        self.assertEqual(reconciled["starting_integrity"], self.baseline())
        self.assertEqual(reconciled["current_debt"], 0)
        self.assertEqual(reconciled["external_debt_reduced"], reconciled["starting_debt"])

    def test_completed_editorial_decision_is_not_reopened_by_reconciliation(self):
        service = CuratorFixSessionService(self.root)
        item = {"item_id": "FIX-EDITORIAL", "finding_type": "editorial_opportunity",
                "classification": "MANUAL", "status": "open", "affected_content": {"task_id": "TASK-1"}}
        created = service.create(started_by="Reviewer", originating_audit_id=None,
                                 queue=[item], baseline=self.baseline())
        service.record(created["session_id"], item["item_id"], "completed")
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.baseline())
        reconciler.planner.build = Mock(return_value=[item])
        reconciled = reconciler.reconcile(created["session_id"], trigger="test")
        self.assertEqual(reconciled["repair_queue"][0]["status"], "completed")

    def test_queue_priority_and_ambiguous_orphan(self):
        report = {"broken_relationships": [],
                  "duplicate_groups": [{"records": [], "confidence": 90, "key": "dup"}],
                  "inventory_mismatches": ["stale"],
                  "orphaned_articles": [{"id": "orphan", "title": "Standalone"}],
                  "missing_review_metadata": [{"id": "legacy", "title": "Legacy"}]}
        queue = CuratorRepairPlanner(self.root).build(report)
        self.assertEqual([item["finding_type"] for item in queue],
                         ["duplicate_group", "inventory_mismatch", "orphaned_article", "legacy_provenance"])
        orphan = next(item for item in queue if item["finding_type"] == "orphaned_article")
        self.assertEqual(orphan["classification"], "AMBIGUOUS")
        self.assertFalse(orphan["safe_automatic"])

    def test_persistent_curator_risks_and_opportunities_are_human_review_only(self):
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = {
            "RISK-1": {"status": "open", "classification": "Risk",
                       "finding_type": "missing_safety_guidance", "priority": "High",
                       "confidence": "medium", "knowledge_debt_score": 4,
                       "content_identifier": "workflow:restart", "title": "Review restart warning"},
            "OPP-1": {"status": "open", "classification": "Opportunity",
                      "finding_type": "article_candidate", "priority": "Medium",
                      "confidence": "high", "knowledge_debt_score": 2,
                      "content_identifier": "workflow:step", "title": "Reusable article"},
        }
        store.save(state)
        report = {"broken_relationships": [], "duplicate_groups": [], "inventory_mismatches": [],
                  "orphaned_articles": [], "missing_review_metadata": []}
        queue = CuratorRepairPlanner(self.root).build(report)
        self.assertEqual([item["finding_type"] for item in queue],
                         ["safety_risk", "editorial_opportunity"])
        self.assertEqual(queue[0]["confidence"], 65.0)
        self.assertEqual(queue[1]["confidence"], 90.0)
        self.assertTrue(all(not item["safe_automatic"] for item in queue))

    def test_supervised_structural_risk_is_admitted_without_reclassification(self):
        self.install_structural_draft()
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        task = self.structural_task()
        state["tasks"] = {task["task_id"]: task}
        store.save(state)
        before = store.load()["tasks"][task["task_id"]]

        queue = CuratorRepairPlanner(self.root).build(self.empty_integrity_report())

        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["finding_type"], "workflow_reasoning_evidence_gap")
        self.assertEqual(item["classification"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(item["affected_content"]["task_id"], task["task_id"])
        self.assertEqual(item["affected_content"]["task_classification"], "Risk")
        self.assertTrue(item["affected_content"]["structural_preview_eligible"])
        self.assertEqual(item["affected_content"]["structural_adapter_id"],
                         "missing_required_upstream_evidence")
        self.assertFalse(item["safe_automatic"])
        self.assertEqual(store.load()["tasks"][task["task_id"]], before)

    def test_preview_ineligible_and_unsupported_structural_risks_remain_excluded(self):
        self.install_structural_draft()
        missing_spec = self.structural_task(task_id="GKT-NO-SPEC", finding_id="CUR-NO-SPEC")
        missing_spec["structured_evidence"]["missing"] = ["unapproved_evidence"]
        unsupported = self.structural_task(
            task_id="GKT-UNSUPPORTED", finding_id="CUR-UNSUPPORTED",
            curator_rule="CUR-UNSUPPORTED", finding_type="unsupported_risk",
        )
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = {
            missing_spec["task_id"]: missing_spec,
            unsupported["task_id"]: unsupported,
        }
        store.save(state)

        queue = CuratorRepairPlanner(self.root).build(self.empty_integrity_report())

        self.assertEqual(queue, [])

    def test_session_reconciliation_adds_new_structural_item_without_task_decisions(self):
        self.install_structural_draft()
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        task = self.structural_task()
        state["tasks"] = {task["task_id"]: task}
        store.save(state)
        task_memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        sessions = CuratorFixSessionService(self.root)
        session = sessions.create(
            started_by="Reviewer", originating_audit_id=None,
            queue=[], baseline=self.empty_integrity_report(),
        )
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value=self.empty_integrity_report())

        reconciled = reconciler.reconcile(session["session_id"], trigger="test")

        matches = [
            item for item in reconciled["repair_queue"]
            if item.get("affected_content", {}).get("task_id") == task["task_id"]
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "open")
        self.assertTrue(matches[0]["introduced_after_start"])
        self.assertEqual(reconciled["outcomes"]["completed"], [])
        self.assertEqual(reconciled["outcomes"]["deferred"], [])
        self.assertEqual(reconciled["outcomes"]["skipped"], [])
        self.assertEqual(reconciled["outcomes"]["rejected"], [])
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(),
                         task_memory_before)
        current = store.load()["tasks"][task["task_id"]]
        self.assertEqual(current["status"], "open")
        self.assertEqual(current["classification"], "Risk")
        self.assertEqual(current["priority"], "Medium")
        self.assertNotIn("owner", current)

    def test_structural_queue_admission_does_not_enable_generic_execution(self):
        registration = CuratorRepairPlanner(self.root).structural_registry.lookup(
            "CUR-WR-TERMINAL-EVIDENCE", "workflow_reasoning_evidence_gap"
        )
        self.assertFalse(registration.executable)
        self.assertTrue(registration.supervised_apply_available)

    def test_current_repository_does_not_mark_unresolved_relationships_safe(self):
        workflow_path = self.root / "app" / "workflow_drafts" / "broken.json"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(json.dumps({
            "workflow_id": "broken", "name": "Broken",
            "nodes": {"step": {"type": "instruction", "title": "Step",
                                "knowledge_article": "missing-article"}},
        }), encoding="utf-8")
        planner = CuratorRepairPlanner(self.root)
        broken = [item for item in planner.build(planner.integrity.report())
                  if item["finding_type"] == "broken_relationship"]
        self.assertGreater(len(broken), 0)
        self.assertTrue(all(item["classification"] == "CREATE_ARTICLE_REQUIRED" for item in broken))
        self.assertTrue(all(not item["safe_automatic"] for item in broken))

    def test_ambiguous_relationship_is_never_safe(self):
        repository = KnowledgeRepository(self.root / "knowledge_base")
        repository.save_published({"id": "printer-one", "title": "Printer Setup", "overview": "One"})
        item = CuratorRepairPlanner(self.root)._relationship(
            {"workflow": "flow", "node": "step", "source": "app/workflow_drafts/flow.json",
             "article": "Unrelated Missing Article"})
        self.assertIn(item["classification"], {"LIKELY_MATCH_REVIEW", "CREATE_ARTICLE_REQUIRED"})
        self.assertFalse(item["safe_automatic"])

    def test_safe_preview_rejects_human_decisions(self):
        executor = CuratorRepairExecutor(self.root)
        for classification in ("MERGE_REVIEW", "LEGACY_REVIEW_REQUIRED", "AMBIGUOUS"):
            item = self.safe_relink()
            item.update({"classification": classification, "safe_automatic": False})
            with self.assertRaises(CuratorRepairError):
                executor.preview(item)

    def test_stale_relationship_is_not_changed(self):
        path = self.root / "app" / "workflow_drafts" / "test.json"
        path.parent.mkdir(parents=True)
        original = {"workflow_id": "test", "nodes": {"step": {"knowledge_article": "Changed"}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(CuratorRepairError):
            CuratorRepairExecutor(self.root).apply(self.safe_relink(), session_id="CFX-000000000001",
                                                   confirmed=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_failed_targeted_validation_rolls_back_relink(self):
        repository = KnowledgeRepository(self.root / "knowledge_base")
        repository.save_published({"id": "canonical-article", "title": "Canonical Article",
                                   "overview": "Canonical"})
        path = self.root / "app" / "workflow_drafts" / "test.json"
        path.parent.mkdir(parents=True)
        original = {"workflow_id": "test", "nodes": {"step": {
            "type": "instruction", "knowledge_article": "Canonical Article"}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        executor = CuratorRepairExecutor(self.root)
        with patch.object(executor.validator, "relationship", return_value={"verified": False}):
            with self.assertRaises(CuratorRepairError):
                executor.apply(self.safe_relink(), session_id="CFX-000000000001", confirmed=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_task_reconciliation_requires_verified_matching_evidence(self):
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = {
            "TASK-1": {"status": "open", "owner": "Curator", "priority": "Critical",
                       "content_identifier": "test step", "title": "Broken test relationship",
                       "history": [], "resolution_history": []},
            "TASK-2": {"status": "open", "owner": "Curator", "priority": "Critical",
                       "content_identifier": "other", "title": "Other issue",
                       "history": [], "resolution_history": []}}
        store.save(state)
        item = self.safe_relink()
        service = CuratorTaskReconciliationService(self.root)
        self.assertEqual(service.reconcile(item, session_id="CFX-000000000001", verified=False), [])
        self.assertEqual(service.reconcile(item, session_id="CFX-000000000001", verified=True), ["TASK-1"])
        current = store.load()
        self.assertEqual(current["tasks"]["TASK-1"]["status"], "resolved")
        self.assertEqual(current["tasks"]["TASK-2"]["status"], "open")
        self.assertIn("CFX-000000000001", current["tasks"]["TASK-1"]["history"][-1]["note"])

    def test_legacy_validation_records_current_truthful_provenance(self):
        repository = KnowledgeRepository(self.root / "knowledge_base")
        repository.save_published({"id": "legacy", "title": "Legacy", "overview": "Content"})
        item = {"classification": "LEGACY_REVIEW_REQUIRED", "affected_content": {"id": "legacy"}}
        result = CuratorRepairExecutor(self.root).approve_legacy_validation(
            item, session_id="CFX-000000000001", reviewer="Current Reviewer", confirmed=True)
        article = repository.get_published_article("legacy")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(article["review"]["review_type"], "legacy_validation")
        self.assertEqual(article["review"]["reviewed_by"], "Current Reviewer")
        self.assertEqual(article["review"]["original_historical_reviewer"], "unknown")
        self.assertTrue(article["review"]["reviewed_at"])

    def test_accessible_templates_have_landmarks_labels_and_keyboard_controls(self):
        templates = Path(__file__).resolve().parents[1] / "app" / "templates"
        wizard = (templates / "curator_fix_wizard.html").read_text(encoding="utf-8")
        start = (templates / "curator_fix_start.html").read_text(encoding="utf-8")
        self.assertIn('id="main-content"', wizard)
        self.assertIn('aria-label="Breadcrumb"', wizard)
        self.assertIn('aria-label="Fix Wizard session status"', wizard)
        self.assertIn('aria-describedby="sessionStatusHelp"', wizard)
        self.assertIn('aria-label="Resume Fix Wizard session', start)
        self.assertIn("Leave Fix Wizard", wizard)
        self.assertIn("Progress is already saved.", wizard)
        self.assertNotIn("Exit / Save Session", wizard)
        self.assertIn('for="reviewer"', start)
        self.assertNotIn("onclick=", wizard)

    def test_progress_separates_original_queue_from_new_findings_and_filter_position(self):
        session = {"original_queue_count": 64, "finding_count": 65,
                   "repair_queue": [
                       {"item_id": "A", "status": "completed", "finding_type": "safety_risk"},
                       {"item_id": "B", "status": "open", "finding_type": "safety_risk"},
                       *[{"item_id": f"X{i}", "status": "resolved_external",
                          "finding_type": "broken_relationship"} for i in range(62)],
                       {"item_id": "NEW", "status": "open", "finding_type": "safety_risk",
                        "introduced_after_start": True},
                   ]}
        progress = CuratorFixSessionService.progress(
            session, category="safety_risk", current_item_id="NEW")
        self.assertEqual(progress["original_queue"], 64)
        self.assertEqual(progress["discovered_during_session"], 1)
        self.assertEqual(progress["filtered_actionable"], 2)
        self.assertEqual(progress["current_position"], 2)

    def test_merge_and_assisted_resolution_preserve_fix_wizard_return_path(self):
        templates = Path(__file__).resolve().parents[1] / "app" / "templates"
        merge = (templates / "knowledge_merge.html").read_text(encoding="utf-8")
        task = (templates / "curator_task_detail.html").read_text(encoding="utf-8")
        package = (templates / "_curator_resolution_package.html").read_text(encoding="utf-8")
        draft = (templates / "draft_review.html").read_text(encoding="utf-8")
        self.assertIn('name="return_to"', merge)
        self.assertIn("task_navigation.return_label", task)
        self.assertIn('name="origin"', task)
        self.assertIn('name="return_to"', package)
        self.assertIn('name="return_to"', draft)

    def test_workflow_article_handoff_preserves_fix_wizard_context(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / "app" / "templates" / "workflow_editor.html").read_text(encoding="utf-8")
        draft = (root / "app" / "templates" / "draft_review.html").read_text(encoding="utf-8")
        published = (root / "app" / "templates" / "published_article.html").read_text(encoding="utf-8")
        app_source = (root / "app" / "app.py").read_text(encoding="utf-8")
        self.assertIn("curator_session=curator_session", workflow)
        self.assertIn("curator_item=curator_item", workflow)
        self.assertIn("Pending workflow relationship", draft)
        self.assertIn("Back to Fix Wizard", published)
        self.assertIn('request.args.get("curator_session"', app_source)


if __name__ == "__main__":
    unittest.main()
