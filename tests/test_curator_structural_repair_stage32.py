import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.repositories.structural_repair_approval_repository import (
    StructuralRepairApprovalRepositoryError,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_evidence_specification_catalog import (
    CuratorEvidenceSpecificationCatalog,
    PRODUCTION_EVIDENCE_SPECIFICATIONS,
)
from app.services.curator_structural_repair_contracts import to_plain_data
from app.services.curator_structural_repair_apply_service import (
    CuratorStructuralRepairApplyService,
    StructuralRepairApplyError,
)
from app.services.curator_structural_repair_approval_service import (
    CuratorStructuralRepairApprovalService,
)
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from app.services.curator_structural_repair_preview_service import CuratorStructuralRepairPreviewService
from app.services.workflow_draft_persistence import LockedWorkflowDraft, WorkflowDraftPersistence
from app.services.workflow_draft_persistence import WorkflowDraftPersistenceError


class StructuralRepairStage32Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.drafts.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "app" / "workflow_drafts" / "network_diagnostics.json"
        self.filename = "network_diagnostics.json"
        (self.drafts / self.filename).write_bytes(source.read_bytes())
        self.task = {
            "task_id": "GKT-STAGE32", "finding_id": "CUR-STAGE32", "status": "open",
            "durable_identity": "terminal-evidence|network_diagnostics:dns_problem",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
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
        self.clock = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self):
        workflow = json.loads((self.drafts / self.filename).read_text(encoding="utf-8"))
        result = CuratorRepairAdapterRegistry().preview(self.task, workflow)
        self.assertTrue(result["available"], result)
        return result

    def issue(self, **changes):
        persistence = WorkflowDraftPersistence(self.drafts)
        with persistence.locked(self.filename) as draft:
            snapshot = draft.read()
        values = {
            "task": self.task, "preview": self.preview(), "snapshot": snapshot,
            "reviewer_identity": "Reviewer", "fix_session_id": "CFX-STAGE32",
            "adapter_id": "missing_required_upstream_evidence",
        }
        values.update(changes)
        return CuratorStructuralRepairApprovalService(
            self.root, now=lambda: self.clock
        ).issue(**values)

    def service(self, **changes):
        values = {"task_loader": lambda _task_id: copy.deepcopy(self.task),
                  "now": lambda: self.clock + timedelta(minutes=1)}
        values.update(changes)
        return CuratorStructuralRepairApplyService(self.root, **values)

    def test_real_preview_approval_and_temporary_draft_apply_exactly_once(self):
        approval = self.issue()
        before_task = copy.deepcopy(self.task)

        result = self.service().apply(
            approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32"
        )
        nodes = result["workflow"]["nodes"]
        self.assertEqual(result["status"], "applied")
        self.assertEqual(nodes["dns_result"]["answers"]["no"]["next"],
                         "test_external_ip_reachability")
        self.assertEqual(nodes["test_external_ip_reachability"]["next"],
                         "external_ip_reachability_result")
        self.assertEqual(nodes["external_ip_reachability_result"]["answers"]
                         ["replies_received"]["next"], "dns_problem")
        self.assertEqual(nodes["external_ip_reachability_result"]["answers"]
                         ["not_established"]["next"], "external_connectivity_unclear")
        history = self.service().applications.get(approval.application_id)
        self.assertEqual([item.outcome for item in history], ["pending", "applied"])
        self.assertEqual(self.service().approvals.get(approval.approval_id)["state"], "consumed")
        self.assertEqual(self.task, before_task)
        repeated = self.service().apply(
            approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32"
        )
        self.assertEqual(repeated["status"], "already_applied")
        self.assertEqual(len(self.service().applications.get(approval.application_id)), 2)

    def test_approval_repository_is_immutable_and_corruption_fails_visibly(self):
        approval = self.issue()
        with self.assertRaises(FileExistsError):
            (self.root / "curation_memory" / "structural_repair_approvals"
             / approval.approval_id).mkdir()
        path = (self.root / "curation_memory" / "structural_repair_approvals"
                / approval.approval_id / "approval.json")
        path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(StructuralRepairApprovalRepositoryError):
            self.service().approvals.get(approval.approval_id)

    def test_missing_expired_and_binding_mismatch_fail_closed(self):
        with self.assertRaises(StructuralRepairApplyError) as missing:
            self.service().apply("SRA-0000000000000000", reviewer_identity="Reviewer",
                                 fix_session_id="CFX-STAGE32")
        self.assertEqual(missing.exception.code, "approval_missing")

        expired = self.issue(lifetime=timedelta(seconds=1))
        with self.assertRaises(StructuralRepairApplyError) as error:
            self.service(now=lambda: self.clock + timedelta(minutes=1)).apply(
                expired.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "approval_expired")

        # Use a separate root because approvals are intentionally one-time artifacts.
        self.tearDown(); self.setUp()
        approval = self.issue()
        with self.assertRaises(StructuralRepairApplyError) as mismatch:
            self.service().apply(approval.approval_id, reviewer_identity="Other",
                                 fix_session_id="CFX-STAGE32")
        self.assertEqual(mismatch.exception.code, "approval_invalid")

    def test_nonactionable_task_and_stale_workflow_invalidate_application(self):
        approval = self.issue()
        closed = copy.deepcopy(self.task); closed["status"] = "resolved"
        with self.assertRaises(StructuralRepairApplyError) as error:
            self.service(task_loader=lambda _: closed).apply(
                approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "approval_invalid")

        self.tearDown(); self.setUp()
        approval = self.issue()
        path = self.drafts / self.filename
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(StructuralRepairApplyError) as stale:
            self.service().apply(approval.approval_id, reviewer_identity="Reviewer",
                                 fix_session_id="CFX-STAGE32")
        self.assertEqual(stale.exception.code, "stale_workflow")
        self.assertEqual(self.service().approvals.get(approval.approval_id)["state"], "invalidated")

    def test_preview_plan_or_specification_substitution_is_rejected(self):
        approval = self.issue()
        path = (self.root / "curation_memory" / "structural_repair_approvals"
                / approval.approval_id / "approval.json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["preview"]["plan"]["plan_id"] = "SRP-SUBSTITUTED"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(StructuralRepairApplyError) as error:
            self.service().apply(approval.approval_id, reviewer_identity="Reviewer",
                                 fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "approval_invalid")

    def test_prewrite_failure_records_failure_without_workflow_write(self):
        approval = self.issue()
        before = (self.drafts / self.filename).read_bytes()
        with self.assertRaises(StructuralRepairApplyError) as error:
            self.service(validator=lambda *_: {"passed": False, "reason": "test"}).apply(
                approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "validation_failed_prewrite")
        self.assertEqual((self.drafts / self.filename).read_bytes(), before)
        history = self.service().applications.get(approval.application_id)
        self.assertEqual(history[-1].failure_category, "validation_failed_prewrite")

    def test_specification_or_current_preview_drift_invalidates_approval(self):
        approval = self.issue()
        raw_spec = to_plain_data(asdict(PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup(
            "external_ip_reachability", 2)))
        raw_spec["evidence_node"]["content"]["instruction"] += " Changed after approval."
        catalog = CuratorEvidenceSpecificationCatalog([raw_spec])
        with self.assertRaises(StructuralRepairApplyError) as changed_spec:
            self.service(specification_catalog=catalog).apply(
                approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(changed_spec.exception.code, "preview_unknown")

        self.tearDown(); self.setUp()
        approval = self.issue()
        changed_task = copy.deepcopy(self.task)
        changed_task["structured_evidence"]["evidence_revision"] = "changed"
        with self.assertRaises(StructuralRepairApplyError) as changed_preview:
            self.service(task_loader=lambda _: changed_task).apply(
                approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(changed_preview.exception.code, "preview_unknown")
        self.assertEqual(self.service().approvals.get(approval.approval_id)["state"], "invalidated")

    def test_cas_stale_and_persistence_failures_are_journaled(self):
        for code, expected in (("stale_workflow", "stale_workflow"),
                               ("persistence_failed", "persistence_failed")):
            with self.subTest(code=code):
                approval = self.issue()
                with patch.object(LockedWorkflowDraft, "replace", side_effect=
                                  WorkflowDraftPersistenceError(code, code)):
                    with self.assertRaises(StructuralRepairApplyError) as error:
                        self.service(validator=lambda *_: {"passed": True}).apply(
                            approval.approval_id, reviewer_identity="Reviewer",
                            fix_session_id="CFX-STAGE32")
                self.assertEqual(error.exception.code, expected)
                history = self.service().applications.get(approval.application_id)
                self.assertEqual([item.outcome for item in history], ["pending", "failed"])
                self.assertEqual(history[-1].failure_category, expected)
                self.tearDown(); self.setUp()

    def test_postwrite_failure_restores_exact_bytes_and_prevents_replay(self):
        approval = self.issue()
        before = (self.drafts / self.filename).read_bytes()
        calls = {"count": 0}
        def validator(*_):
            calls["count"] += 1
            return {"passed": calls["count"] == 1}
        result = self.service(validator=validator).apply(
            approval.approval_id, reviewer_identity="Reviewer", fix_session_id="CFX-STAGE32")
        self.assertEqual(result["status"], "rollback_succeeded")
        self.assertEqual((self.drafts / self.filename).read_bytes(), before)
        self.assertEqual(self.service().approvals.get(approval.approval_id)["state"], "invalidated")
        with self.assertRaises(StructuralRepairApplyError):
            self.service().apply(approval.approval_id, reviewer_identity="Reviewer",
                                 fix_session_id="CFX-STAGE32")

    def test_rollback_failure_is_recorded_and_requires_intervention(self):
        approval = self.issue()
        calls = {"count": 0}
        def validator(*_):
            calls["count"] += 1
            return {"passed": calls["count"] == 1}
        with patch.object(LockedWorkflowDraft, "restore", side_effect=
                          WorkflowDraftPersistenceError("restore_failed", "restore blocked")):
            with self.assertRaises(StructuralRepairApplyError) as error:
                self.service(validator=validator).apply(
                    approval.approval_id, reviewer_identity="Reviewer",
                    fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "rollback_failed")
        history = self.service().applications.get(approval.application_id)
        self.assertEqual(history[-1].failure_category, "rollback_failed")
        self.assertEqual(history[-1].rollback_status, "failed")
        self.assertEqual(self.service().approvals.get(approval.approval_id)["state"], "invalidated")

    def test_pending_recovery_classifies_before_after_and_unexpected_states(self):
        approval = self.issue()
        service = self.service(validator=lambda *_: {"passed": True})
        preview = self.preview()
        workflow = json.loads((self.drafts / self.filename).read_text(encoding="utf-8"))
        candidate = CuratorStructuralRepairPreviewService().simulate(workflow, preview)
        candidate_bytes = (json.dumps(candidate, indent=4, ensure_ascii=False) + "\n").encode()
        expected_raw = StructuralRepairFingerprint.raw_workflow(candidate_bytes)
        expected_semantic = StructuralRepairFingerprint.semantic_workflow(candidate)
        service._append(approval, preview, {"passed": True}, outcome="pending",
                        expected_raw=expected_raw, expected_semantic=expected_semantic)
        self.assertEqual(service.classify_pending(approval.application_id), "before_state")
        with service.persistence.locked(self.filename) as draft:
            draft.replace(approval.workflow_raw_sha256_before, candidate_bytes)
        self.assertEqual(service.classify_pending(approval.application_id), "expected_after_state")
        (self.drafts / self.filename).write_bytes(candidate_bytes + b" ")
        self.assertEqual(service.classify_pending(approval.application_id), "unexpected_state")

    def test_lock_unavailable_is_retryable_and_creates_no_journal(self):
        approval = self.issue()
        service = self.service()
        with patch.object(service.persistence, "locked", side_effect=WorkflowDraftPersistenceError(
                "lock_unavailable", "busy")):
            with self.assertRaises(StructuralRepairApplyError) as error:
                service.apply(approval.approval_id, reviewer_identity="Reviewer",
                              fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "lock_unavailable")
        self.assertEqual(service.approvals.get(approval.approval_id)["state"], "approved")
        self.assertEqual(service.applications.get(approval.application_id), ())

    def test_builtin_and_publication_targets_cannot_be_approved(self):
        approval = self.issue()
        path = (self.root / "curation_memory" / "structural_repair_approvals"
                / approval.approval_id / "approval.json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        for lifecycle, workflow_path in (
                ("built_in", "app/workflows/network_diagnostics.json"),
                ("published", "workflow_publications/network_diagnostics/v1.json")):
            changed = copy.deepcopy(raw)
            changed["approval"]["workflow_lifecycle"] = lifecycle
            changed["approval"]["workflow_path"] = workflow_path
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(StructuralRepairApplyError) as error:
                self.service().apply(approval.approval_id, reviewer_identity="Reviewer",
                                     fix_session_id="CFX-STAGE32")
            self.assertEqual(error.exception.code, "approval_invalid")
        self.assertEqual((self.drafts / self.filename).is_file(), True)

    def test_malformed_caller_input_fails_closed(self):
        with self.assertRaises(StructuralRepairApplyError) as error:
            self.service().apply("../../workflow.json", reviewer_identity="Reviewer",
                                 fix_session_id="CFX-STAGE32")
        self.assertEqual(error.exception.code, "approval_invalid")

    def test_no_production_execution_projection_or_task_mutation(self):
        registration = CuratorRepairAdapterRegistry().lookup(
            "CUR-WR-TERMINAL-EVIDENCE", "workflow_reasoning_evidence_gap")
        self.assertFalse(registration.executable)
        self.assertEqual(self.task["status"], "open")


if __name__ == "__main__":
    unittest.main()
