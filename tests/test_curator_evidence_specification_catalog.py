import copy
import json
import operator
import unittest
from pathlib import Path

from app.services.curator_evidence_specification_catalog import (
    CuratorEvidenceSpecificationCatalog,
    EvidenceSpecificationCatalogError,
    EXTERNAL_IP_REACHABILITY_SPECIFICATION,
    EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2,
    PRODUCTION_EVIDENCE_SPECIFICATIONS,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_contracts import ImmutableMapping, to_plain_data
from app.services.curator_structural_repair_preview_service import CuratorStructuralRepairPreviewService
from app.services.workflow_quality_validator import WorkflowQualityValidator
from app.services.workflow_validation_service import WorkflowValidationService
from curator.workflow_reasoning import WorkflowReasoningAuditor
from tests.structural_repair_fixtures import pre_stage34_network_diagnostics_bytes


class CuratorEvidenceSpecificationCatalogTests(unittest.TestCase):
    def test_external_ip_specification_is_explicit_stable_and_immutable(self):
        v1 = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability", 1)
        specification = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability")

        self.assertEqual(v1.specification_id, "external-ip-reachability-windows-v1")
        self.assertEqual(v1.version, 1)
        self.assertEqual(specification.specification_id, "external-ip-reachability-windows-v2")
        self.assertEqual(specification.version, 2)
        self.assertTrue(specification.approved)
        with self.assertRaises(TypeError):
            specification.evidence_node.content["instruction"] = "Changed"
        with self.assertRaises(TypeError):
            specification.result_node.content["answers"]["replies_received"]["next"] = "Changed"
        with self.assertRaises(TypeError):
            specification.outcome_nodes[0].content["message"] = "Changed"
        self.assertIsInstance(specification.evidence_node.content, ImmutableMapping)
        self.assertNotIsInstance(specification.evidence_node.content, dict)
        with self.assertRaises(TypeError):
            operator.ior(specification.evidence_node.content, {"instruction": "Changed"})
        with self.assertRaises(TypeError):
            dict.__setitem__(specification.evidence_node.content, "instruction", "Changed")
        with self.assertRaises(AttributeError):
            specification.evidence_node.content.update({"instruction": "Changed"})
        with self.assertRaises(AttributeError):
            specification.outcome_nodes[0].required_evidence.append("Changed")
        self.assertIsInstance(
            specification.result_node.content["answers"]["replies_received"], ImmutableMapping
        )

    def test_registered_content_is_detached_from_source_and_versions_are_independent(self):
        source = copy.deepcopy(EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2)
        catalog = CuratorEvidenceSpecificationCatalog((source,))
        registered = catalog.lookup("external_ip_reachability")
        original_message = registered.outcome_nodes[0].content["message"]
        original_instruction = registered.evidence_node.content["instruction"]

        source["outcome_nodes"][0]["content"]["message"] = "Mutated source message."
        source["evidence_node"]["content"]["instruction"] = "Mutated source instruction."

        self.assertEqual(registered.outcome_nodes[0].content["message"], original_message)
        self.assertEqual(registered.evidence_node.content["instruction"], original_instruction)
        v1 = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability", 1)
        v2 = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability", 2)
        self.assertIsNot(v1.evidence_node.content, v2.evidence_node.content)

    def test_latest_version_selection_is_independent_of_source_order(self):
        forward = CuratorEvidenceSpecificationCatalog((
            EXTERNAL_IP_REACHABILITY_SPECIFICATION,
            EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2,
        ))
        reverse = CuratorEvidenceSpecificationCatalog((
            EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2,
            EXTERNAL_IP_REACHABILITY_SPECIFICATION,
        ))

        self.assertEqual(forward.lookup("external_ip_reachability").specification_id,
                         "external-ip-reachability-windows-v2")
        self.assertEqual(reverse.lookup("external_ip_reachability").specification_id,
                         "external-ip-reachability-windows-v2")

    def test_v1_remains_auditable_and_v2_is_fully_resolved(self):
        v1 = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability", 1)
        v2 = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability", 2)

        self.assertNotEqual(v1.specification_id, v2.specification_id)
        self.assertEqual((v1.version, v2.version), (1, 2))
        self.assertIn("$reviewed_external_reachability_failure_destination",
                      dict(v1.result_routes).values())
        self.assertNotIn("$reviewed_external_reachability_failure_destination",
                         json.dumps(EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2))
        self.assertEqual(v2.outcome_nodes[0].node_id, "external_connectivity_unclear")

    def test_unknown_runtime_task_or_growth_data_cannot_register_specification(self):
        registry = CuratorRepairAdapterRegistry()
        task = {
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "structured_evidence": {"missing": ["runtime_generated_evidence"]},
            "evidence_specification": EXTERNAL_IP_REACHABILITY_SPECIFICATION,
            "growth_proposal": {"kind": "repair_adapter", "status": "enabled"},
        }

        self.assertIsNone(PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("runtime_generated_evidence"))
        self.assertIsNone(registry.evidence_specification("runtime_generated_evidence"))
        self.assertEqual(registry.eligibility(task)["status"], "missing_evidence_specification")
        self.assertFalse(hasattr(PRODUCTION_EVIDENCE_SPECIFICATIONS, "register"))

    def test_duplicate_identity_version_and_evidence_key_are_rejected(self):
        duplicate = copy.deepcopy(EXTERNAL_IP_REACHABILITY_SPECIFICATION)
        with self.assertRaises(EvidenceSpecificationCatalogError):
            CuratorEvidenceSpecificationCatalog((
                EXTERNAL_IP_REACHABILITY_SPECIFICATION, duplicate,
            ))

        second_identity = copy.deepcopy(EXTERNAL_IP_REACHABILITY_SPECIFICATION)
        second_identity["specification_id"] = "another-approved-probe"
        with self.assertRaises(EvidenceSpecificationCatalogError):
            CuratorEvidenceSpecificationCatalog((
                EXTERNAL_IP_REACHABILITY_SPECIFICATION, second_identity,
            ))

    def test_malformed_or_unapproved_production_specification_is_rejected(self):
        malformed = copy.deepcopy(EXTERNAL_IP_REACHABILITY_SPECIFICATION)
        malformed["result_node"]["content"]["answers"] = {}
        unapproved = copy.deepcopy(EXTERNAL_IP_REACHABILITY_SPECIFICATION)
        unapproved.update({"approved": False, "approved_by": "", "approved_at": ""})

        with self.assertRaises(EvidenceSpecificationCatalogError):
            CuratorEvidenceSpecificationCatalog((malformed,))
        with self.assertRaises(EvidenceSpecificationCatalogError):
            CuratorEvidenceSpecificationCatalog((unapproved,))

    def test_specification_is_non_destructive_and_does_not_conflate_dns_with_reachability(self):
        specification = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability")
        text = json.dumps(to_plain_data({
            "action": specification.evidence_node.content,
            "result": specification.result_node.content,
        })).casefold()

        self.assertIn("ping -n 4", text)
        self.assertIn("organization-approved external ip", text)
        self.assertIn("do not change dns or network settings", text)
        self.assertIn("without relying on dns name resolution", text)
        self.assertIn("no reply may also reflect filtering or blocked icmp", text)
        for prohibited in ("flushdns", "netsh", "set-dns", "change the dns server", "dns success proves"):
            self.assertNotIn(prohibited, text)

    def test_real_advanced_network_topology_produces_valid_three_node_preview(self):
        workflow_path = Path("app/workflow_drafts/network_diagnostics.json")
        workflow_before = workflow_path.read_bytes()
        workflow = json.loads(pre_stage34_network_diagnostics_bytes().decode("utf-8"))
        observation = next(
            item for item in WorkflowReasoningAuditor().analyze(workflow)
            if item.rule == "CUR-WR-TERMINAL-EVIDENCE" and item.node_id == "dns_problem"
        )
        task = {
            "task_id": "GKT-REAL-TOPOLOGY", "finding_id": "CUR-REAL-TOPOLOGY",
            "curator_rule": observation.rule, "finding_type": observation.finding_type,
            "content_type": "workflow_node",
            "content_identifier": f"{workflow['workflow_id']}:{observation.node_id}",
            "structured_evidence": observation.structural,
        }
        registry = CuratorRepairAdapterRegistry()

        eligibility = registry.eligibility(task)
        first = registry.preview(task, workflow)
        second = registry.preview(task, workflow)
        simulated = CuratorStructuralRepairPreviewService().simulate(workflow, first)
        schema = WorkflowValidationService().validate(simulated)
        quality = WorkflowQualityValidator().validate(simulated)
        remaining = [item for item in WorkflowReasoningAuditor().analyze(simulated)
                     if item.rule == "CUR-WR-TERMINAL-EVIDENCE"
                     and item.node_id == "dns_problem"]

        self.assertEqual(eligibility["status"], "preview_candidate")
        self.assertTrue(eligibility["capability_eligible"])
        self.assertFalse(eligibility["preview_eligible"])
        self.assertFalse(eligibility["execution_eligible"])
        self.assertTrue(first["available"])
        self.assertTrue(first["preview_eligible"])
        self.assertEqual(first["status"], "preview_eligible")
        self.assertFalse(first["execution_eligible"])
        self.assertEqual(first["before"]["predecessor_edges"], [{
            "source": "dns_result", "route": "No", "destination": "dns_problem",
        }])
        self.assertEqual([item["node_id"] for item in first["proposed"]["inserted_nodes"]], [
            "test_external_ip_reachability", "external_ip_reachability_result",
            "external_connectivity_unclear",
        ])
        self.assertEqual(first["proposed"]["result_routes"], [
            {"answer": "replies_received", "destination": "dns_problem",
             "preserves_terminal": True},
            {"answer": "not_established", "destination": "external_connectivity_unclear",
             "preserves_terminal": False},
        ])
        terminal_text = first["proposed"]["outcome_nodes"][0]["content"]["message"]
        self.assertIn("does not distinguish a DNS problem", terminal_text)
        self.assertNotIn("internet connectivity is unavailable", terminal_text.casefold())
        self.assertTrue(schema["is_valid"], schema)
        self.assertEqual(quality["overall_status"], "CLEAN", quality)
        self.assertEqual(quality["metrics"]["reachable_nodes"], 15)
        self.assertEqual(quality["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(quality["metrics"]["cycles_detected"], 0)
        self.assertEqual(remaining, [])
        self.assertEqual(simulated["nodes"]["dns_result"]["answers"]["yes"]["next"],
                         workflow["nodes"]["dns_result"]["answers"]["yes"]["next"])
        self.assertEqual(first, second)
        self.assertEqual(workflow_path.read_bytes(), workflow_before)


if __name__ == "__main__":
    unittest.main()
