import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.services.curator_action_verification_specification_catalog import (
    PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_contracts import (
    ActionVerificationRepairPlan,
    ActionVerificationSpecification,
    StructuralRepairContractError,
)
from app.services.curator_structural_repair_preview_service import (
    CuratorStructuralRepairPreviewService,
)
from curator.checks import CuratorChecks
from curator.models import AuditFilter, InventoryRecord
from curator.tasks import KnowledgeTaskService
from curator.workflow_reasoning import WorkflowReasoningAuditor


class CuratorPostActionVerificationPreviewTests(unittest.TestCase):
    @staticmethod
    def workflow():
        return {
            "workflow_id": "vpn_connectivity_win",
            "name": "VPN Connectivity Troubleshooting (Windows)",
            "start_node": "start",
            "progress_mode": "branch_aware",
            "nodes": {
                "start": {
                    "type": "question",
                    "question": "Was an approved security configuration applied?",
                    "answers": {
                        "yes": {"label": "Yes", "next": "instr_configure_fw_av"},
                        "no": {"label": "No", "next": "instr_check_adapter_status"},
                    },
                },
                "instr_configure_fw_av": {
                    "type": "instruction",
                    "title": "Configure Security Software for the VPN",
                    "instruction": (
                        "Keep security enabled. Configure only an approved VPN allow rule, "
                        "then retry the VPN connection."
                    ),
                    "next": "res_vpn_resolved",
                },
                "instr_check_adapter_status": {
                    "type": "instruction",
                    "title": "Inspect Adapter Status",
                    "instruction": "Inspect the VPN adapter status without changing settings.",
                    "next": "escalate",
                },
                "res_vpn_resolved": {
                    "type": "resolution",
                    "title": "VPN Resolved",
                    "message": "The VPN connection was restored.",
                },
                "escalate": {
                    "type": "resolution",
                    "title": "Further Review",
                    "message": "Further diagnostics are required.",
                },
            },
        }

    @classmethod
    def finding(cls):
        return next(
            item for item in WorkflowReasoningAuditor().analyze(cls.workflow())
            if item.rule == "CUR-WR-ACTION-VERIFICATION"
        )

    @classmethod
    def task(cls):
        finding = cls.finding()
        return {
            "task_id": "GKT-ACTION-FIXTURE",
            "finding_id": "CUR-ACTION-FIXTURE",
            "durable_identity": (
                "CUR-WR-ACTION-VERIFICATION|workflow_node|"
                "vpn_connectivity_win:instr_configure_fw_av|"
                "workflow_reasoning_unverified_action"
            ),
            "curator_rule": finding.rule,
            "finding_type": finding.finding_type,
            "content_type": "workflow_node",
            "content_identifier": "vpn_connectivity_win:instr_configure_fw_av",
            "structured_evidence": copy.deepcopy(finding.structural),
        }

    def test_detector_emits_typed_exact_action_edge_evidence(self):
        evidence = self.finding().structural

        self.assertEqual(evidence["evidence_version"], "1.0")
        self.assertEqual(evidence["workflow_id"], "vpn_connectivity_win")
        self.assertEqual(evidence["action_node_id"], "instr_configure_fw_av")
        self.assertEqual(evidence["action_node_type"], "instruction")
        self.assertEqual(evidence["outgoing_edge"], {
            "source": "instr_configure_fw_av",
            "route": "next",
            "destination": "res_vpn_resolved",
        })
        self.assertEqual(
            evidence["verification_key"],
            "vpn_approved_security_configuration_result",
        )
        self.assertEqual(evidence["required_destinations"], [
            "instr_check_adapter_status", "res_vpn_resolved",
        ])

    def test_normal_reconciliation_preserves_stable_task_identity_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            record = InventoryRecord(
                "workflow", "vpn_connectivity_win", "VPN",
                "app/workflow_drafts/vpn_connectivity_win.json",
                "Networking", "Windows", "draft", self.workflow(),
            )
            finding = next(
                item for item in CuratorChecks(Path(directory)).run_record(record)
                if item.rule == "CUR-WR-ACTION-VERIFICATION"
            )
            state = {"tasks": {}}
            tasks = KnowledgeTaskService()
            first = tasks.reconcile(
                state, [finding], [record], run_id="AUD-1",
                observed_at="2026-08-25T00:00:00+00:00", filters=AuditFilter(),
            )
            second = tasks.reconcile(
                state, [finding], [record], run_id="AUD-2",
                observed_at="2026-08-25T01:00:00+00:00", filters=AuditFilter(),
            )

        task_id = first["created"][0]
        self.assertEqual(second["created"], [])
        self.assertEqual(second["observed"], [task_id])
        self.assertEqual(
            state["tasks"][task_id]["structured_evidence"]["outgoing_edge"],
            finding.structured_evidence["outgoing_edge"],
        )

    def test_supported_pattern_builds_exact_read_only_validated_preview(self):
        workflow = self.workflow()
        task = self.task()
        before_workflow = json.dumps(workflow, sort_keys=True)
        before_task = json.dumps(task, sort_keys=True)

        preview = CuratorRepairAdapterRegistry().preview(task, workflow)

        self.assertTrue(preview["available"])
        self.assertTrue(preview["read_only"])
        self.assertTrue(preview["validation"]["passed"])
        self.assertTrue(preview["validation"]["original_finding_absent"])
        self.assertEqual(preview["validation"]["new_reasoning_findings"], [])
        self.assertEqual(
            [item["node_id"] for item in preview["proposed"]["inserted_nodes"]],
            ["q_configured_fw_av_works"],
        )
        self.assertEqual(preview["proposed"]["changed_predecessor_edges"], [{
            "before": {
                "source": "instr_configure_fw_av", "route": "next",
                "destination": "res_vpn_resolved",
            },
            "after": {
                "source": "instr_configure_fw_av", "route": "next",
                "destination": "q_configured_fw_av_works",
            },
        }])
        self.assertEqual(json.dumps(workflow, sort_keys=True), before_workflow)
        self.assertEqual(json.dumps(task, sort_keys=True), before_task)

    def test_plan_contains_only_approved_question_and_routes(self):
        preview = CuratorRepairAdapterRegistry().preview(self.task(), self.workflow())
        plan = ActionVerificationRepairPlan.from_dict(preview["plan"])

        self.assertEqual(plan.action_node_id, "instr_configure_fw_av")
        self.assertEqual(plan.specification.verification_node.node_id,
                         "q_configured_fw_av_works")
        self.assertEqual(set(plan.new_edges), {
            type(plan.outgoing_edge)("q_configured_fw_av_works", "yes", "res_vpn_resolved"),
            type(plan.outgoing_edge)("q_configured_fw_av_works", "no", "instr_check_adapter_status"),
            type(plan.outgoing_edge)("q_configured_fw_av_works", "unsure", "instr_check_adapter_status"),
        })

    def test_ambiguous_and_stale_action_edges_are_rejected(self):
        ambiguous = self.workflow()
        ambiguous["nodes"]["instr_configure_fw_av"]["skip_to"] = "escalate"
        stale = self.workflow()
        stale["nodes"]["instr_configure_fw_av"]["next"] = "instr_check_adapter_status"

        ambiguous_result = CuratorRepairAdapterRegistry().preview(self.task(), ambiguous)
        stale_result = CuratorRepairAdapterRegistry().preview(self.task(), stale)

        self.assertFalse(ambiguous_result["available"])
        self.assertIn("exactly one", ambiguous_result["reason"])
        self.assertFalse(stale_result["available"])
        self.assertIn("stale", stale_result["reason"])

    def test_unsupported_family_and_mismatched_specification_fail_closed(self):
        unsupported = self.workflow()
        unsupported["workflow_id"] = "other_workflow"
        unsupported_finding = next(
            item for item in WorkflowReasoningAuditor().analyze(unsupported)
            if item.rule == "CUR-WR-ACTION-VERIFICATION"
        )
        unsupported_task = self.task()
        unsupported_task["content_identifier"] = "other_workflow:instr_configure_fw_av"
        unsupported_task["structured_evidence"] = unsupported_finding.structural
        mismatch = self.task()
        mismatch["structured_evidence"]["verification_key"] = "unsupported_key"

        unsupported_eligibility = CuratorRepairAdapterRegistry().eligibility(unsupported_task)
        mismatch_eligibility = CuratorRepairAdapterRegistry().eligibility(mismatch)

        self.assertEqual(unsupported_eligibility["status"], "human_review_only")
        self.assertEqual(mismatch_eligibility["status"], "missing_evidence_specification")

    def test_existing_equivalent_verification_is_not_duplicated(self):
        workflow = self.workflow()
        specification = (
            PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS.lookup(
                "vpn_approved_security_configuration_result"
            )
        )
        workflow["nodes"][specification.verification_node.node_id] = copy.deepcopy(
            dict(specification.verification_node.content)
        )

        result = CuratorRepairAdapterRegistry().preview(self.task(), workflow)

        self.assertFalse(result["available"])
        self.assertIn("already exists", result["reason"])

    def test_generic_execution_and_production_apply_remain_disabled(self):
        registry = CuratorRepairAdapterRegistry()
        registration = registry.lookup(
            "CUR-WR-ACTION-VERIFICATION", "workflow_reasoning_unverified_action"
        )
        eligibility = registry.eligibility(self.task())

        self.assertFalse(registration.executable)
        self.assertFalse(registration.supervised_apply_available)
        self.assertFalse(eligibility["execution_eligible"])
        self.assertFalse(eligibility["supervised_apply_available"])

    def test_incomplete_or_ambiguous_action_plans_are_rejected(self):
        preview = CuratorRepairAdapterRegistry().preview(self.task(), self.workflow())
        invalid = copy.deepcopy(preview["plan"])
        invalid["new_edges"].append({
            "source": "q_configured_fw_av_works",
            "route": "unexpected",
            "destination": "escalate",
        })

        with self.assertRaises(StructuralRepairContractError):
            ActionVerificationRepairPlan.from_dict(invalid)

    def test_catalog_specification_is_approved_and_immutable(self):
        specification = PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS.lookup(
            "vpn_approved_security_configuration_result"
        )

        self.assertIsInstance(specification, ActionVerificationSpecification)
        self.assertTrue(specification.approved)
        with self.assertRaises(TypeError):
            specification.verification_node.content["question"] = "Replacement"


if __name__ == "__main__":
    unittest.main()
