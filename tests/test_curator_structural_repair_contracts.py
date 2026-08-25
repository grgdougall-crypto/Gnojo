import copy
import unittest

from app.services.curator_repair_adapter_registry import (
    CuratorRepairAdapterRegistry,
    RepairAdapterRegistration,
)
from app.services.curator_structural_repair_contracts import (
    EvidenceProbeSpecification,
    OutcomeNodeSpecification,
    StructuralRepairContractError,
    StructuralRepairPlan,
)


class CuratorStructuralRepairContractTests(unittest.TestCase):
    @staticmethod
    def probe_data(*, approved=True):
        return {
            "specification_id": "probe-external-evidence-v1",
            "version": 1,
            "evidence_key": "external_evidence",
            "approved": approved,
            "approved_by": "Reviewer" if approved else "",
            "approved_at": "2026-08-24T00:00:00+00:00" if approved else "",
            "evidence_node": {
                "node_id": "collect_external_evidence",
                "content": {
                    "type": "instruction", "title": "Collect external evidence",
                    "instruction": "Perform the approved evidence check and record the result.",
                    "next": "external_evidence_result",
                },
            },
            "result_node": {
                "node_id": "external_evidence_result",
                "content": {
                    "type": "question", "question": "What was the approved evidence result?",
                    "answers": {
                        "supports_terminal": {"label": "Supports this result", "next": "terminal"},
                        "does_not_support": {"label": "Does not support this result", "next": "other"},
                    },
                },
            },
            "result_routes": {
                "supports_terminal": "terminal",
                "does_not_support": "other",
            },
        }

    @classmethod
    def plan_data(cls):
        return {
            "plan_id": "PLAN-1", "workflow_id": "flow", "terminal_id": "terminal",
            "required_evidence_key": "external_evidence",
            "affected_paths": [{
                "nodes": ["start", "decision", "terminal"],
                "missing": ["external_evidence"],
                "predecessor_edge": {"source": "decision", "route": "no", "destination": "terminal"},
            }],
            "predecessor_edges": [
                {"source": "decision", "route": "no", "destination": "terminal"},
            ],
            "probe": cls.probe_data(),
            "proposed_outcome_nodes": [],
            "preserved_terminal": "terminal",
            "changed_existing_edges": [
                {"source": "decision", "route": "no", "destination": "terminal"},
            ],
            "new_edges": [
                {"source": "collect_external_evidence", "route": "next",
                 "destination": "external_evidence_result"},
                {"source": "external_evidence_result", "route": "supports_terminal",
                 "destination": "terminal"},
                {"source": "external_evidence_result", "route": "does_not_support",
                 "destination": "other"},
            ],
            "preserved_existing_nodes": ["start", "decision", "terminal", "other"],
            "unaffected_routes": [
                {"source": "decision", "route": "yes", "destination": "other"},
            ],
            "expected_post_repair": {
                "rule": "CUR-WR-TERMINAL-EVIDENCE", "status": "finding_absent",
            },
        }

    def test_registry_projects_existing_and_unknown_adapters_without_execution(self):
        registry = CuratorRepairAdapterRegistry()
        existing = registry.lookup("CUR-REL-ARTICLE-CANDIDATE", "article_candidate")
        structural = registry.lookup(
            "CUR-WR-TERMINAL-EVIDENCE", "workflow_reasoning_evidence_gap"
        )

        self.assertEqual(existing.adapter_id, "canonical_article_link")
        self.assertTrue(existing.executable)
        self.assertEqual(structural.adapter_id, "missing_required_upstream_evidence")
        self.assertFalse(structural.executable)
        self.assertEqual(registry.eligibility({"curator_rule": "UNKNOWN", "finding_type": "none"})["status"],
                         "human_review_only")

    def test_terminal_evidence_remains_ineligible_without_matching_specification(self):
        result = CuratorRepairAdapterRegistry().eligibility({
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "structured_evidence": {"missing": ["external_evidence"]},
        })
        self.assertEqual(result["status"], "missing_evidence_specification")
        self.assertFalse(result["preview_eligible"])
        self.assertFalse(result["execution_eligible"])

    def test_executable_structural_adapter_still_requires_approved_specification(self):
        registration = RepairAdapterRegistration(
            "test-structural", "CUR-WR-TERMINAL-EVIDENCE",
            "workflow_reasoning_evidence_gap", executable=True, structural=True,
        )
        task = {
            "curator_rule": registration.curator_rule,
            "finding_type": registration.finding_type,
            "structured_evidence": {"missing": ["external_evidence"]},
        }
        missing = CuratorRepairAdapterRegistry([registration]).eligibility(task)
        unapproved = EvidenceProbeSpecification.from_dict(self.probe_data(approved=False))
        still_missing = CuratorRepairAdapterRegistry([registration], [unapproved]).eligibility(task)
        approved = EvidenceProbeSpecification.from_dict(self.probe_data())
        eligible = CuratorRepairAdapterRegistry([registration], [approved]).eligibility(task)

        self.assertEqual(missing["status"], "missing_evidence_specification")
        self.assertEqual(still_missing["status"], "missing_evidence_specification")
        self.assertEqual(eligible["status"], "preview_candidate")
        self.assertTrue(eligible["capability_eligible"])
        self.assertFalse(eligible["preview_eligible"])

    def test_complete_probe_and_plan_are_typed_and_valid(self):
        probe = EvidenceProbeSpecification.from_dict(self.probe_data())
        plan = StructuralRepairPlan.from_dict(self.plan_data())

        self.assertTrue(probe.approved)
        self.assertEqual(plan.workflow_id, "flow")
        self.assertEqual(plan.predecessor_edges[0].route, "no")
        self.assertEqual(plan.expected_post_repair_status, "finding_absent")

    def test_optional_generic_resolution_outcome_is_typed_and_immutable(self):
        value = self.probe_data()
        value["result_node"]["content"]["answers"]["does_not_support"]["next"] = "unclear"
        value["result_routes"]["does_not_support"] = "unclear"
        value["outcome_nodes"] = [{
            "node_id": "unclear",
            "terminal_semantics": "bounded_uncertainty",
            "required_evidence": ["external_evidence_not_established"],
            "content": {
                "type": "resolution", "title": "Evidence Was Inconclusive",
                "message": "Record the result and request an appropriate technical review.",
            },
        }]

        probe = EvidenceProbeSpecification.from_dict(value)

        self.assertIsInstance(probe.outcome_nodes[0], OutcomeNodeSpecification)
        with self.assertRaises(TypeError):
            probe.outcome_nodes[0].content["message"] = "Changed"

    def test_incomplete_unsupported_nonterminal_or_placeholder_outcomes_fail(self):
        base = self.probe_data()
        base["result_node"]["content"]["answers"]["does_not_support"]["next"] = "unclear"
        base["result_routes"]["does_not_support"] = "unclear"
        valid = {
            "node_id": "unclear", "terminal_semantics": "bounded_uncertainty",
            "required_evidence": ["external_evidence_not_established"],
            "content": {"type": "resolution", "title": "Unclear", "message": "Review evidence."},
        }
        cases = []
        for mutate in (
            lambda item: item["content"].pop("message"),
            lambda item: item["content"].update({"type": "instruction", "instruction": "Run it."}),
            lambda item: item["content"].update({"next": "another"}),
            lambda item: item.update({"required_evidence": []}),
            lambda item: item["content"].update({"message": "$unresolved"}),
        ):
            value = copy.deepcopy(base)
            outcome = copy.deepcopy(valid)
            mutate(outcome)
            value["outcome_nodes"] = [outcome]
            cases.append(value)
        unresolved = copy.deepcopy(base)
        unresolved["outcome_nodes"] = [copy.deepcopy(valid)]
        unresolved["result_node"]["content"]["answers"]["does_not_support"]["next"] = "$reviewed_route"
        unresolved["result_routes"]["does_not_support"] = "$reviewed_route"
        cases.append(unresolved)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(StructuralRepairContractError):
                    EvidenceProbeSpecification.from_dict(value)

    def test_incomplete_or_ambiguous_plans_are_rejected(self):
        cases = []
        missing_paths = self.plan_data()
        missing_paths["affected_paths"] = []
        cases.append(missing_paths)
        mismatched_edge = self.plan_data()
        mismatched_edge["predecessor_edges"][0]["route"] = "yes"
        cases.append(mismatched_edge)
        missing_result = self.plan_data()
        del missing_result["probe"]["result_routes"]["does_not_support"]
        cases.append(missing_result)
        wrong_probe = self.plan_data()
        wrong_probe["probe"]["evidence_key"] = "different_evidence"
        cases.append(wrong_probe)
        no_preserved_terminal_route = self.plan_data()
        no_preserved_terminal_route["probe"]["result_routes"]["supports_terminal"] = "other"
        no_preserved_terminal_route["probe"]["result_node"]["content"]["answers"]["supports_terminal"]["next"] = "other"
        cases.append(no_preserved_terminal_route)
        dangling_result = self.plan_data()
        dangling_result["probe"]["result_routes"]["does_not_support"] = "missing"
        dangling_result["probe"]["result_node"]["content"]["answers"]["does_not_support"]["next"] = "missing"
        dangling_result["new_edges"][2]["destination"] = "missing"
        cases.append(dangling_result)
        extra_edge = self.plan_data()
        extra_edge["new_edges"].append({
            "source": "external_evidence_result", "route": "unexpected",
            "destination": "other",
        })
        cases.append(extra_edge)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(StructuralRepairContractError):
                    StructuralRepairPlan.from_dict(value)


if __name__ == "__main__":
    unittest.main()
