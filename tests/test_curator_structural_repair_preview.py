import copy
import json
import unittest

from app.services.curator_repair_adapter_registry import (
    CuratorRepairAdapterRegistry,
    RepairAdapterRegistration,
)
from app.services.curator_structural_repair_contracts import EvidenceProbeSpecification
from app.services.curator_structural_repair_preview_service import (
    CuratorStructuralRepairPreviewService,
    StructuralRepairPreviewError,
)


class CuratorStructuralRepairPreviewTests(unittest.TestCase):
    @staticmethod
    def workflow():
        return {
            "workflow_id": "flow", "name": "Flow", "start_node": "start",
            "nodes": {
                "start": {
                    "type": "question", "question": "What was observed?",
                    "answers": {
                        "yes": {"label": "Yes", "next": "other"},
                        "no": {"label": "No", "next": "terminal"},
                    },
                },
                "other": {"type": "resolution", "title": "Other", "message": "Other result."},
                "terminal": {"type": "resolution", "title": "Terminal", "message": "Diagnosis."},
            },
        }

    @staticmethod
    def task():
        return {
            "task_id": "GKT-STRUCTURAL", "finding_id": "CUR-STRUCTURAL",
            "durable_identity": "CUR-WR-TERMINAL-EVIDENCE|workflow_node|flow:terminal|workflow_reasoning_evidence_gap",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node", "content_identifier": "flow:terminal",
            "structured_evidence": {
                "requirement": "diagnostic_scope", "terminal": "terminal",
                "missing": ["external_evidence"], "affected_path_count": 1,
                "affected_paths": [{
                    "nodes": ["start", "terminal"], "missing": ["external_evidence"],
                    "predecessor_edge": {
                        "source": "start", "route": "No", "destination": "terminal",
                    },
                }],
                "predecessor_edges": [{
                    "source": "start", "route": "No", "destination": "terminal",
                }],
            },
        }

    @staticmethod
    def specification_data(*, version=1, approved=True, instruction=None):
        return {
            "specification_id": "approved-external-evidence",
            "version": version, "evidence_key": "external_evidence",
            "approved": approved,
            "approved_by": "Reviewer" if approved else "",
            "approved_at": "2026-08-24T00:00:00+00:00" if approved else "",
            "evidence_node": {
                "node_id": "collect_external_evidence",
                "content": {
                    "type": "instruction", "title": "Collect external evidence",
                    "instruction": instruction or "Perform the approved evidence check and record the result.",
                    "next": "external_evidence_result",
                },
            },
            "result_node": {
                "node_id": "external_evidence_result",
                "content": {
                    "type": "question", "question": "What was the approved evidence result?",
                    "answers": {
                        "supports": {"label": "Supports the diagnosis", "next": "$preserved_terminal"},
                        "does_not_support": {"label": "Does not support the diagnosis", "next": "other"},
                    },
                },
            },
            "result_routes": {
                "supports": "$preserved_terminal", "does_not_support": "other",
            },
        }

    @classmethod
    def specification(cls, **changes):
        return EvidenceProbeSpecification.from_dict(cls.specification_data(**changes))

    @classmethod
    def outcome_specification(cls, *, message="The evidence was inconclusive. Request review."):
        value = cls.specification_data(version=2)
        value["specification_id"] = "approved-external-evidence-v2"
        value["result_node"]["content"]["answers"]["does_not_support"]["next"] = "unclear"
        value["result_routes"]["does_not_support"] = "unclear"
        value["outcome_nodes"] = [{
            "node_id": "unclear", "terminal_semantics": "bounded_uncertainty",
            "required_evidence": ["external_evidence_not_established"],
            "content": {
                "type": "resolution", "title": "Evidence Was Inconclusive",
                "message": message,
            },
        }]
        return EvidenceProbeSpecification.from_dict(value)

    @classmethod
    def registry(cls, specification=None):
        registration = RepairAdapterRegistration(
            "missing_required_upstream_evidence", "CUR-WR-TERMINAL-EVIDENCE",
            "workflow_reasoning_evidence_gap", executable=True, structural=True,
        )
        return CuratorRepairAdapterRegistry(
            [registration], [specification or cls.specification()]
        )

    def test_registry_builds_exact_read_only_preview(self):
        workflow = self.workflow()
        task = self.task()
        before_workflow = json.dumps(workflow, sort_keys=True)
        before_task = json.dumps(task, sort_keys=True)

        preview = self.registry().preview(task, workflow)

        self.assertTrue(preview["available"])
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview["before"]["predecessor_edges"], [{
            "source": "start", "route": "No", "destination": "terminal",
        }])
        self.assertEqual(preview["before"]["current_destinations"], ["terminal"])
        self.assertEqual(preview["before"]["terminal"]["node_id"], "terminal")
        self.assertEqual(preview["proposed"]["changed_predecessor_edges"], [{
            "before": {"source": "start", "route": "No", "destination": "terminal"},
            "after": {
                "source": "start", "route": "No", "destination": "collect_external_evidence",
            },
        }])
        self.assertEqual(
            [item["node_id"] for item in preview["proposed"]["inserted_nodes"]],
            ["collect_external_evidence", "external_evidence_result"],
        )
        self.assertEqual(preview["proposed"]["result_routes"], [
            {"answer": "supports", "destination": "terminal", "preserves_terminal": True},
            {"answer": "does_not_support", "destination": "other", "preserves_terminal": False},
        ])
        self.assertEqual(preview["proposed"]["preserved_terminal_route"]["destination"], "terminal")
        self.assertIn(
            {"source": "start", "route": "Yes", "destination": "other"},
            preview["proposed"]["unaffected_routes"],
        )
        self.assertEqual(json.dumps(workflow, sort_keys=True), before_workflow)
        self.assertEqual(json.dumps(task, sort_keys=True), before_task)

    def test_repeated_preview_and_plan_construction_are_deterministic(self):
        first = self.registry().preview(self.task(), self.workflow())
        second = self.registry().preview(self.task(), self.workflow())
        self.assertEqual(first, second)

    def test_proposed_ids_are_collision_safe_and_repeatable(self):
        workflow = self.workflow()
        workflow["nodes"]["collect_external_evidence"] = {
            "type": "resolution", "title": "Existing", "message": "Existing."
        }
        workflow["nodes"]["external_evidence_result"] = {
            "type": "resolution", "title": "Existing result", "message": "Existing."
        }

        first = self.registry().preview(self.task(), workflow)
        second = self.registry().preview(self.task(), workflow)
        ids = [item["node_id"] for item in first["proposed"]["inserted_nodes"]]

        self.assertEqual(first, second)
        self.assertTrue(ids[0].startswith("collect_external_evidence_"))
        self.assertTrue(ids[1].startswith("external_evidence_result_"))
        self.assertTrue(set(ids).isdisjoint(workflow["nodes"]))

    def test_generic_outcome_preview_distinguishes_nodes_and_edges(self):
        preview = self.registry(self.outcome_specification()).preview(self.task(), self.workflow())

        self.assertTrue(preview["available"])
        self.assertEqual([item["node_id"] for item in preview["proposed"]["inserted_nodes"]], [
            "collect_external_evidence", "external_evidence_result", "unclear",
        ])
        self.assertEqual(preview["proposed"]["outcome_nodes"][0]["terminal_semantics"],
                         "bounded_uncertainty")
        self.assertEqual(preview["proposed"]["changed_existing_edges"], [{
            "source": "start", "route": "No", "destination": "terminal",
        }])
        self.assertEqual(preview["proposed"]["new_edges"], [
            {"source": "collect_external_evidence", "route": "next",
             "destination": "external_evidence_result"},
            {"source": "external_evidence_result", "route": "supports",
             "destination": "terminal"},
            {"source": "external_evidence_result", "route": "does_not_support",
             "destination": "unclear"},
        ])
        self.assertEqual(preview["proposed"]["preserved_existing_nodes"],
                         ["other", "start", "terminal"])

    def test_outcome_id_is_collision_safe_and_fingerprinted(self):
        workflow = self.workflow()
        workflow["nodes"]["unclear"] = {
            "type": "resolution", "title": "Existing", "message": "Existing outcome."
        }
        first = self.registry(self.outcome_specification()).preview(self.task(), workflow)
        second = self.registry(self.outcome_specification()).preview(self.task(), workflow)
        changed = self.registry(self.outcome_specification(message="Different governed guidance.")).preview(
            self.task(), workflow
        )

        outcome_id = first["proposed"]["outcome_nodes"][0]["node_id"]
        self.assertTrue(outcome_id.startswith("unclear_"))
        self.assertNotIn(outcome_id, workflow["nodes"])
        self.assertEqual(first, second)
        self.assertNotEqual(first["preview_token"], changed["preview_token"])

    def test_unknown_non_outcome_destination_fails_closed(self):
        value = self.specification_data()
        value["result_node"]["content"]["answers"]["does_not_support"]["next"] = "missing"
        value["result_routes"]["does_not_support"] = "missing"
        result = self.registry(EvidenceProbeSpecification.from_dict(value)).preview(
            self.task(), self.workflow()
        )

        self.assertFalse(result["available"])
        self.assertIn("unknown workflow node", result["reason"])

    def test_ambiguous_or_stale_edge_evidence_is_rejected(self):
        ambiguous = self.workflow()
        ambiguous["nodes"]["start"]["answers"]["another_no"] = {
            "label": "No", "next": "terminal",
        }
        stale = self.workflow()
        stale["nodes"]["start"]["answers"]["no"]["next"] = "other"

        ambiguous_result = self.registry().preview(self.task(), ambiguous)
        stale_result = self.registry().preview(self.task(), stale)

        self.assertFalse(ambiguous_result["available"])
        self.assertIn("exactly once", ambiguous_result["reason"])
        self.assertFalse(stale_result["available"])
        self.assertIn("exactly once", stale_result["reason"])

    def test_unapproved_or_missing_specification_cannot_preview(self):
        task = self.task()
        registration = RepairAdapterRegistration(
            "missing_required_upstream_evidence", "CUR-WR-TERMINAL-EVIDENCE",
            "workflow_reasoning_evidence_gap", executable=True, structural=True,
        )
        missing = CuratorRepairAdapterRegistry([registration]).preview(task, self.workflow())
        unapproved = CuratorRepairAdapterRegistry(
            [registration], [self.specification(approved=False)]
        ).preview(task, self.workflow())

        self.assertFalse(missing["available"])
        self.assertEqual(missing["status"], "missing_evidence_specification")
        self.assertFalse(unapproved["available"])
        self.assertEqual(unapproved["status"], "missing_evidence_specification")

    def test_fingerprint_covers_workflow_specification_and_task_evidence(self):
        base = self.registry().preview(self.task(), self.workflow())["preview_token"]
        changed_workflow = self.workflow()
        changed_workflow["nodes"]["other"]["message"] = "Changed immutable preview input."
        workflow_token = self.registry().preview(self.task(), changed_workflow)["preview_token"]
        changed_spec = self.specification(version=2)
        spec_token = self.registry(changed_spec).preview(
            self.task(), self.workflow()
        )["preview_token"]
        changed_task = copy.deepcopy(self.task())
        changed_task["structured_evidence"]["evidence_revision"] = "new"
        task_token = self.registry().preview(changed_task, self.workflow())["preview_token"]

        self.assertEqual(len({base, workflow_token, spec_token, task_token}), 4)

    def test_builder_rejects_nonmatching_workflow_and_incomplete_paths(self):
        service = CuratorStructuralRepairPreviewService()
        wrong_workflow = self.workflow()
        wrong_workflow["workflow_id"] = "other-flow"
        incomplete = self.task()
        incomplete["structured_evidence"]["affected_paths"] = []

        with self.assertRaises(StructuralRepairPreviewError):
            service.build(self.task(), self.specification(), wrong_workflow)
        with self.assertRaises(StructuralRepairPreviewError):
            service.build(incomplete, self.specification(), self.workflow())

    def test_production_registry_has_external_ip_spec_but_no_structural_execution(self):
        registry = CuratorRepairAdapterRegistry()
        task = self.task()
        task["structured_evidence"]["missing"] = ["external_ip_reachability"]

        self.assertIsNotNone(registry.evidence_specification("external_ip_reachability"))
        eligibility = registry.eligibility(task)
        result = registry.preview(task, self.workflow())
        self.assertEqual(eligibility["status"], "preview_candidate")
        self.assertTrue(eligibility["capability_eligible"])
        self.assertTrue(eligibility["supervised_apply_available"])
        self.assertFalse(result["available"])
        self.assertFalse(result["preview_eligible"])
        self.assertFalse(result["execution_eligible"])


if __name__ == "__main__":
    unittest.main()
