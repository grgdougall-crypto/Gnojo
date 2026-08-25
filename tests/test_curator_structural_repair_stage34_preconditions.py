import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from app.app import app as flask_app
from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
)
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_apply_service import (
    CuratorStructuralRepairApplyService,
)
from app.services.curator_structural_repair_approval_service import (
    CuratorStructuralRepairApprovalService,
)
from app.services.curator_structural_repair_recovery_service import (
    CuratorStructuralRepairRecoveryService,
    StructuralRepairRecoveryError,
)
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryStore
from curator.models import AuditFilter, InventoryRecord
from curator.tasks import KnowledgeTaskService


class StructuralRepairStage34PreconditionTests(unittest.TestCase):
    task_id = "GKT-STAGE34"
    finding_id = "CUR-STAGE34"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.drafts.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "app/workflow_drafts/network_diagnostics.json"
        self.filename = "network_diagnostics.json"
        (self.drafts / self.filename).write_bytes(source.read_bytes())
        self.original = (self.drafts / self.filename).read_bytes()
        self.publication = self.root / "app/workflow_publications/network_diagnostics/current.json"
        self.publication.parent.mkdir(parents=True)
        self.publication.write_bytes(b'{"version":4}\n')
        self.published_before = self.publication.read_bytes()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"][self.task_id] = self.task()
        self.store.save(state)
        self.sessions = CuratorFixSessionService(self.root)
        self.fix_session = self.sessions.create(
            started_by="Stage 3.4 Reviewer", originating_audit_id=None,
            queue=[self.queue_item()], baseline={"counts": {}},
        )
        self.approval = CuratorStructuralRepairApprovalService(self.root).issue(
            task_id=self.task_id, workflow_filename=self.filename,
            reviewer_identity="Stage 3.4 Reviewer",
            fix_session_id=self.fix_session["session_id"],
        )

    def tearDown(self):
        self.temporary.cleanup()

    @classmethod
    def task(cls):
        return {
            "task_id": cls.task_id, "finding_id": cls.finding_id, "status": "open",
            "owner": "Curator", "priority": "Medium", "classification": "Recommendation",
            "confidence": "high", "knowledge_debt_score": 5, "times_observed": 1,
            "first_seen": "2026-08-25T00:00:00+00:00",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "title": "Terminal diagnosis may exceed collected evidence",
            "explanation": "The terminal requires evidence not collected on this path.",
            "recommended_action": "Review the governed structural repair.",
            "durable_identity": (
                "CUR-WR-TERMINAL-EVIDENCE|workflow_node|"
                "network_diagnostics:dns_problem|workflow_reasoning_evidence_gap|"
                "draft|app/workflow_drafts/network_diagnostics.json"
            ),
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
            "related_workflows": ["network_diagnostics"], "history": [], "evidence": [],
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

    @classmethod
    def queue_item(cls):
        return {
            "item_id": "FIX-STAGE34", "status": "open",
            "classification": "STRUCTURAL_REVIEW_REQUIRED",
            "finding_type": "workflow_reasoning_evidence_gap", "knowledge_debt": 5,
            "affected_content": {"task_id": cls.task_id},
        }

    def apply(self):
        return CuratorStructuralRepairApplyService(self.root).apply(
            self.approval.approval_id,
            reviewer_identity="Stage 3.4 Reviewer",
            fix_session_id=self.fix_session["session_id"],
        )

    def recovery_service(self):
        return CuratorStructuralRepairRecoveryService(self.root)

    def test_apply_retains_exact_bytes_and_supervised_restore_is_cas_guarded(self):
        applied = self.apply()
        changed = (self.drafts / self.filename).read_bytes()
        self.assertNotEqual(changed, self.original)
        recovery_repository = StructuralRepairRecoveryRepository(
            self.root / "curation_memory"
        )
        material = recovery_repository.get(self.approval.application_id)
        self.assertEqual(material["original_bytes"], self.original)
        self.assertEqual(
            material["expected_workflow_raw_sha256_after"],
            applied["application"]["expected_workflow_raw_sha256_after"],
        )
        application_repository = StructuralRepairApplicationRepository(
            self.root / "curation_memory"
        )
        original_application_history = [
            item.to_dict() for item in application_repository.get(self.approval.application_id)
        ]

        result = self.recovery_service().restore(
            self.approval.application_id,
            reviewer_identity="Stage 3.4 Reviewer",
            fix_session_id=self.fix_session["session_id"],
            reason="Designer acceptance found an unacceptable route presentation.",
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual((self.drafts / self.filename).read_bytes(), self.original)
        self.assertEqual(result["recovery_event"]["restored_raw_sha256"],
                         material["workflow_raw_sha256_before"])
        self.assertEqual(result["recovery_event"]["restored_semantic_sha256"],
                         material["workflow_semantic_sha256_before"])
        self.assertEqual(
            [item.to_dict() for item in application_repository.get(self.approval.application_id)],
            original_application_history,
        )
        events = recovery_repository.events(self.approval.application_id)
        self.assertEqual([item["outcome"] for item in events], ["pending", "recovered"])
        self.assertEqual(events[-1]["reason"],
                         "Designer acceptance found an unacceptable route presentation.")
        self.assertEqual(self.publication.read_bytes(), self.published_before)
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")

    def test_restore_refuses_changed_draft_and_identity_mismatches(self):
        self.apply()
        path = self.drafts / self.filename
        path.write_bytes(path.read_bytes() + b" ")
        changed = path.read_bytes()
        with self.assertRaisesRegex(StructuralRepairRecoveryError, "changed after application"):
            self.recovery_service().restore(
                self.approval.application_id,
                reviewer_identity="Stage 3.4 Reviewer",
                fix_session_id=self.fix_session["session_id"], reason="Acceptance failed.",
            )
        self.assertEqual(path.read_bytes(), changed)
        with self.assertRaises(StructuralRepairRecoveryError):
            self.recovery_service().restore(
                self.approval.application_id,
                reviewer_identity="Wrong Reviewer",
                fix_session_id=self.fix_session["session_id"], reason="Acceptance failed.",
            )
        self.assertEqual(StructuralRepairRecoveryRepository(
            self.root / "curation_memory"
        ).events(self.approval.application_id), ())

    def test_restore_refuses_mismatched_application_workflow_and_task_identity(self):
        self.apply()
        service = self.recovery_service()
        with self.assertRaises(StructuralRepairRecoveryError):
            service.restore(
                "SRX-0000000000000000", reviewer_identity="Stage 3.4 Reviewer",
                fix_session_id=self.fix_session["session_id"], reason="Acceptance failed.",
            )

        material_path = (
            self.root / "curation_memory/structural_repair_recoveries"
            / self.approval.application_id / "material.json"
        )
        material = json.loads(material_path.read_text(encoding="utf-8"))
        material["workflow_id"] = "other_workflow"
        material_path.write_text(json.dumps(material, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(StructuralRepairRecoveryError, "does not match"):
            service.restore(
                self.approval.application_id, reviewer_identity="Stage 3.4 Reviewer",
                fix_session_id=self.fix_session["session_id"], reason="Acceptance failed.",
            )
        material["workflow_id"] = "network_diagnostics"
        material_path.write_text(json.dumps(material, indent=2) + "\n", encoding="utf-8")

        state = self.store.load()
        state["tasks"][self.task_id]["finding_id"] = "CUR-OTHER"
        self.store.save(state)
        with self.assertRaisesRegex(StructuralRepairRecoveryError, "authority"):
            service.restore(
                self.approval.application_id, reviewer_identity="Stage 3.4 Reviewer",
                fix_session_id=self.fix_session["session_id"], reason="Acceptance failed.",
            )
        self.assertNotEqual((self.drafts / self.filename).read_bytes(), self.original)

    def test_browser_restore_requires_confirmation_and_uses_server_bound_identity(self):
        self.apply()
        previous_root = flask_app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        flask_app.config.update(TESTING=True, STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root))
        try:
            client = flask_app.test_client()
            url = (
                f"/curator/structural-repairs/{self.approval.application_id}/restore"
                f"?curator_session={self.fix_session['session_id']}&origin=maintenance"
                f"&return_to=/curator/fix/{self.fix_session['session_id']}%3Fitem%3DFIX-STAGE34"
            )
            page = client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Restore Pre-Repair Draft", page.data)
            self.assertIn(b"published workflow is unaffected", page.data.lower())
            token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1)
            missing_confirmation = client.post(url, data={
                "csrf_token": token, "curator_session": self.fix_session["session_id"],
                "reason": "Browser acceptance failed.",
            })
            self.assertEqual(missing_confirmation.status_code, 400)
            response = client.post(url, data={
                "csrf_token": token, "curator_session": self.fix_session["session_id"],
                "confirmed": "yes", "reason": "Browser acceptance failed.",
                "workflow_path": "app/workflow_drafts/other.json", "task_id": "GKT-OTHER",
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"editable draft was restored exactly", response.data)
            self.assertEqual((self.drafts / self.filename).read_bytes(), self.original)
        finally:
            if previous_root is None:
                flask_app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
            else:
                flask_app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = previous_root

    def test_normal_reconciliation_preserves_identity_and_enables_read_only_preview(self):
        workflow = json.loads(self.original.decode("utf-8"))
        record = InventoryRecord(
            "workflow", "network_diagnostics", "Advanced Network Diagnostics",
            "app/workflow_drafts/network_diagnostics.json", "Networking", "Windows",
            "draft", workflow,
        )
        finding = next(
            item for item in CuratorChecks(self.root).run_record(record)
            if item.rule == "CUR-WR-TERMINAL-EVIDENCE"
            and item.content_identifier == "network_diagnostics:dns_problem"
        )
        task = self.task()
        task.pop("structured_evidence")
        task["finding_id"] = finding.identifier
        task["durable_identity"] = KnowledgeTaskService.durable_identity(finding)
        state = {"tasks": {self.task_id: task}}
        before = bytes(self.original)

        result = KnowledgeTaskService().reconcile(
            state, [finding], [record], run_id="AUD-STAGE34",
            observed_at="2026-08-25T12:00:00+00:00",
            filters=AuditFilter(content_type="workflow"),
        )

        self.assertEqual(result["created"], [])
        self.assertEqual(list(state["tasks"]), [self.task_id])
        refreshed = state["tasks"][self.task_id]
        self.assertEqual(refreshed["finding_id"], finding.identifier)
        self.assertEqual(refreshed["status"], "open")
        self.assertEqual(refreshed["structured_evidence"]["terminal"], "dns_problem")
        self.assertEqual(refreshed["structured_evidence"]["predecessor_edges"], [{
            "source": "dns_result", "route": "No", "destination": "dns_problem",
        }])
        registry = CuratorRepairAdapterRegistry()
        self.assertTrue(registry.eligibility(refreshed)["capability_eligible"])
        preview = registry.preview(refreshed, workflow)
        self.assertTrue(preview["available"])
        self.assertTrue(preview["preview_eligible"])
        self.assertEqual(preview["status"], "preview_eligible")
        self.assertEqual(
            [item["node_id"] for item in preview["proposed"]["inserted_nodes"]],
            ["test_external_ip_reachability", "external_ip_reachability_result",
             "external_connectivity_unclear"],
        )
        self.assertEqual((self.drafts / self.filename).read_bytes(), before)
        self.assertFalse(CuratorRepairAdapterRegistry().lookup(
            "CUR-WR-TERMINAL-EVIDENCE", "workflow_reasoning_evidence_gap"
        ).executable)


if __name__ == "__main__":
    unittest.main()
