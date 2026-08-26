import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_apply_service import (
    CuratorStructuralRepairApplyService,
)
from app.services.curator_structural_repair_approval_service import (
    CuratorStructuralRepairApprovalService,
)
from app.services.curator_structural_repair_contracts import (
    ProgressMetadataRepairPlan,
    StructuralRepairContractError,
)
from app.services.curator_structural_repair_governance import (
    StructuralRepairFingerprint,
)


class CuratorProgressMetadataRepairTests(unittest.TestCase):
    task_id = "GKT-PROGRESS"
    finding_id = "CUR-PROGRESS"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.drafts.mkdir(parents=True)
        self.path = self.drafts / "higher_layer_connectivity.json"
        self.path.write_bytes(self.workflow_bytes(self.workflow()))
        self.original = self.path.read_bytes()
        self.task_value = self.task()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow():
        nodes = {}
        for step in range(1, 6):
            destination = f"step_{step + 1}" if step < 5 else "done"
            nodes[f"step_{step}"] = {
                "type": "question",
                "question": f"Check {step}?",
                "answers": {"yes": {"label": "Yes", "next": destination}},
            }
        nodes["done"] = {
            "type": "resolution", "title": "Done", "message": "Review complete."
        }
        return {
            "workflow_id": "higher_layer_connectivity",
            "name": "Higher-Layer Connectivity Diagnostics",
            "category": "Networking",
            "platform": "Cross-platform",
            "estimated_steps": 4,
            "start_node": "step_1",
            "nodes": nodes,
        }

    @classmethod
    def task(cls):
        return {
            "task_id": cls.task_id,
            "finding_id": cls.finding_id,
            "status": "open",
            "classification": "Opportunity",
            "curator_rule": "CUR-WR-PROGRESS",
            "finding_type": "workflow_reasoning_progress_inconsistency",
            "content_type": "workflow",
            "content_identifier": "higher_layer_connectivity",
            "structured_evidence": {
                "configured_steps": 4,
                "maximum_user_visible_nodes": 6,
            },
        }

    @staticmethod
    def workflow_bytes(workflow):
        return (json.dumps(workflow, indent=4, ensure_ascii=False) + "\n").encode("utf-8")

    def preview(self, workflow=None, raw=None):
        workflow = workflow or json.loads(self.path.read_text(encoding="utf-8"))
        raw = raw or self.workflow_bytes(workflow)
        return CuratorRepairAdapterRegistry().preview(
            self.task_value,
            workflow,
            workflow_raw_sha256=hashlib.sha256(raw).hexdigest(),
            workflow_semantic_sha256=StructuralRepairFingerprint.semantic_workflow(workflow),
        )

    def test_exact_absent_to_branch_aware_preview_is_read_only(self):
        workflow = self.workflow()
        before = copy.deepcopy(workflow)

        preview = self.preview(workflow)
        plan = ProgressMetadataRepairPlan.from_dict(preview["plan"])

        self.assertTrue(preview["available"])
        self.assertTrue(preview["read_only"])
        self.assertFalse(plan.before_present)
        self.assertIsNone(plan.before_value)
        self.assertEqual(plan.metadata_path, "/progress_mode")
        self.assertEqual(plan.after_value, "branch_aware")
        self.assertEqual(plan.workflow_raw_sha256_before,
                         hashlib.sha256(self.workflow_bytes(workflow)).hexdigest())
        self.assertTrue(preview["validation"]["passed"])
        self.assertTrue(preview["validation"]["graph_unchanged"])
        self.assertTrue(preview["validation"]["estimated_steps_unchanged"])
        self.assertEqual(workflow, before)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_stale_metadata_and_current_branch_aware_are_rejected(self):
        stale = self.workflow()
        stale["estimated_steps"] = 5
        branch_aware = self.workflow()
        branch_aware["progress_mode"] = "branch_aware"

        stale_result = self.preview(stale)
        no_op = self.preview(branch_aware)

        self.assertFalse(stale_result["available"])
        self.assertIn("stale", stale_result["reason"])
        self.assertFalse(no_op["available"])
        self.assertIn("already enabled", no_op["reason"])

    def test_explicit_static_mode_is_within_the_same_narrow_allowlist(self):
        workflow = self.workflow()
        workflow["progress_mode"] = "static"

        preview = self.preview(workflow)
        plan = ProgressMetadataRepairPlan.from_dict(preview["plan"])

        self.assertTrue(preview["available"])
        self.assertTrue(plan.before_present)
        self.assertEqual(plan.before_value, "static")
        self.assertEqual(plan.after_value, "branch_aware")

    def test_plan_rejects_other_paths_and_after_values(self):
        preview = self.preview()
        wrong_path = copy.deepcopy(preview["plan"])
        wrong_path["metadata_path"] = "/estimated_steps"
        wrong_value = copy.deepcopy(preview["plan"])
        wrong_value["after_value"] = "static"

        with self.assertRaises(StructuralRepairContractError):
            ProgressMetadataRepairPlan.from_dict(wrong_path)
        with self.assertRaises(StructuralRepairContractError):
            ProgressMetadataRepairPlan.from_dict(wrong_value)

    def test_supervised_service_apply_reuses_journal_and_exact_byte_recovery(self):
        task_before = copy.deepcopy(self.task_value)
        approval_service = CuratorStructuralRepairApprovalService._for_test(
            self.root, task_loader=lambda task_id: self.task_value,
        )
        approval = approval_service.issue(
            task_id=self.task_id,
            workflow_filename=self.path.name,
            reviewer_identity="Stage 3.7 Reviewer",
            fix_session_id="CFX-STAGE37",
        )
        result = CuratorStructuralRepairApplyService._for_test(
            self.root, task_loader=lambda task_id: self.task_value,
        ).apply(
            approval.approval_id,
            reviewer_identity="Stage 3.7 Reviewer",
            fix_session_id="CFX-STAGE37",
        )

        applied = json.loads(self.path.read_text(encoding="utf-8"))
        expected = self.workflow()
        expected["progress_mode"] = "branch_aware"
        self.assertEqual(result["status"], "applied")
        self.assertEqual(applied, expected)
        self.assertEqual(applied["estimated_steps"], 4)
        self.assertEqual(applied["nodes"], self.workflow()["nodes"])
        self.assertEqual(self.task_value, task_before)
        history = StructuralRepairApplicationRepository(
            self.root / "curation_memory"
        ).get(approval.application_id)
        self.assertEqual(history[-1].outcome, "applied")
        self.assertEqual(history[-1].proposed_node_ids, ())
        self.assertEqual(history[-1].changed_edges, ())
        self.assertEqual(history[-1].new_edges, ())
        self.assertEqual(dict(history[-1].metadata_changes[0]), {
            "path": "/progress_mode",
            "before_present": False,
            "before_value": None,
            "after_value": "branch_aware",
        })
        recovery = StructuralRepairRecoveryRepository(
            self.root / "curation_memory"
        ).get(approval.application_id)
        self.assertEqual(recovery["original_bytes"], self.original)
        self.assertEqual(
            recovery["expected_workflow_raw_sha256_after"],
            result["application"]["expected_workflow_raw_sha256_after"],
        )

    def test_registry_exposes_only_supervised_browser_authority(self):
        registry = CuratorRepairAdapterRegistry()
        registration = registry.lookup(
            "CUR-WR-PROGRESS", "workflow_reasoning_progress_inconsistency"
        )
        eligibility = registry.eligibility(self.task_value)

        self.assertFalse(registration.executable)
        self.assertTrue(registration.supervised_apply_available)
        self.assertFalse(eligibility["execution_eligible"])
        self.assertTrue(eligibility["supervised_apply_available"])


if __name__ == "__main__":
    unittest.main()
