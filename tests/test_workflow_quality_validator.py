import unittest
from copy import deepcopy

from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_quality_validator import WorkflowQualityValidator
from app.services.workflow_validation_service import WorkflowValidationService


def workflow(nodes=None, **values):
    result = {
        "workflow_id": "quality_fixture",
        "name": "Quality fixture",
        "estimated_steps": 3,
        "start_node": "start",
        "nodes": nodes or {
            "start": {
                "type": "question",
                "question": "Ready?",
                "answers": {
                    "yes": {"label": "Yes", "next": "act"},
                    "no": {"label": "No", "next": "done"},
                },
            },
            "act": {
                "type": "instruction",
                "title": "Act",
                "instruction": "Perform one bounded action.",
                "next": "done",
            },
            "done": {"type": "resolution", "title": "Done", "message": "Finished."},
        },
    }
    result.update(values)
    return result


class WorkflowQualityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = WorkflowQualityValidator()

    def test_valid_acyclic_workflow_and_path_metrics(self):
        report = self.validator.validate(workflow(progress_mode="branch_aware"))
        self.assertEqual(report["overall_status"], "CLEAN")
        self.assertEqual(report["metrics"]["shortest_path"], 2)
        self.assertEqual(report["metrics"]["longest_path"], 3)
        self.assertEqual(report["metrics"]["cycles_detected"], 0)
        self.assertNotIn("ERROR", {item["severity"] for item in report["findings"]})

    def test_unreachable_node_is_reported(self):
        value = workflow()
        value["nodes"]["orphan"] = {
            "type": "resolution", "title": "Orphan", "message": "Unused."
        }
        report = self.validator.validate(value)
        self.assertFinding(report, "UNREACHABLE_NODE", "orphan")

    def test_missing_destination_is_reported(self):
        value = workflow()
        value["nodes"]["act"]["next"] = "missing"
        report = self.validator.validate(value)
        self.assertFinding(report, "MISSING_BRANCH_DESTINATION", "act", "ERROR")

        value["nodes"]["start"]["answers"]["yes"].pop("next")
        report = self.validator.validate(value)
        self.assertFinding(report, "MISSING_BRANCH_DESTINATION", "start", "ERROR")

    def test_cycle_and_nonterminating_branch_are_cycle_safe(self):
        value = workflow()
        value["nodes"]["act"]["next"] = "act"
        report = self.validator.validate(value)
        self.assertFinding(report, "CYCLE_DETECTED", "act", "ERROR")
        self.assertFinding(report, "NONTERMINATING_PATH", "act", "ERROR")

    def test_terminal_with_ordinary_outgoing_branch_is_reported(self):
        value = workflow()
        value["nodes"]["done"]["next"] = "act"
        report = self.validator.validate(value)
        self.assertFinding(report, "TERMINAL_OUTGOING_BRANCH", "done", "ERROR")

    def test_static_progress_detects_premature_completion(self):
        value = workflow(estimated_steps=2)
        report = self.validator.validate(value)
        self.assertFinding(report, "PREMATURE_STATIC_PROGRESS", "act", "ERROR")
        self.assertFinding(report, "STATIC_PATH_LENGTH_CONFLICT")

    def test_valid_branch_aware_progress_supports_short_and_long_paths(self):
        value = workflow(progress_mode="branch_aware")
        report = self.validator.validate(value)
        self.assertNotIn(
            "BRANCH_PROGRESS_INTEGRITY", {item["rule"] for item in report["findings"]}
        )
        self.assertEqual(report["metrics"]["shortest_path"], 2)
        self.assertEqual(report["metrics"]["longest_path"], 3)

    def test_bounded_uncertainty_path_is_safe(self):
        value = workflow()
        value["nodes"]["start"]["answers"]["unsure"] = {
            "label": "I'm not sure", "next": "done"
        }
        report = self.validator.validate(value)
        self.assertNotIn(
            "UNBOUNDED_UNCERTAINTY_BRANCH", {item["rule"] for item in report["findings"]}
        )

    def test_uncertainty_loop_is_reported(self):
        value = workflow()
        value["nodes"]["start"]["answers"]["unsure"] = {
            "label": "I'm not sure", "next": "act"
        }
        value["nodes"]["act"]["next"] = "start"
        report = self.validator.validate(value)
        self.assertFinding(report, "UNBOUNDED_UNCERTAINTY_BRANCH", "start", "ERROR")

    def test_immediate_repeated_remediation_is_flagged_for_review(self):
        value = workflow()
        value["nodes"]["act"]["next"] = "second_action"
        value["nodes"]["second_action"] = {
            "type": "instruction", "title": "Second", "instruction": "Act again.",
            "next": "done",
        }
        report = self.validator.validate(value)
        self.assertFinding(report, "REPEATED_REMEDIATION_WITHOUT_EVIDENCE", "act", "WARNING")
        self.assertFinding(report, "ACTION_WITHOUT_VERIFICATION", "act", "WARNING")

    def test_cross_workflow_handoff_can_be_checked_with_catalog_context(self):
        value = workflow(nodes={
            "start": {
                "type": "transition", "title": "Continue", "message": "Next phase.",
                "next_workflow": "missing_workflow",
            }
        }, estimated_steps=1)
        report = self.validator.validate(value, available_workflow_ids={"quality_fixture"})
        self.assertFinding(report, "BROKEN_WORKFLOW_HANDOFF", "start", "ERROR")
        clean = self.validator.validate(
            value, available_workflow_ids={"quality_fixture", "missing_workflow"}
        )
        self.assertNotIn("BROKEN_WORKFLOW_HANDOFF", {item["rule"] for item in clean["findings"]})

    def test_known_progress_regressions_and_corrected_publications(self):
        publications = WorkflowPublicationService()
        for workflow_id, old_estimate in (
            ("application_crash", 8),
            ("bt_win_not_connecting", 10),
        ):
            with self.subTest(workflow_id=workflow_id):
                current = publications.load_current(workflow_id)["workflow"]
                corrected = self.validator.validate(current)
                self.assertNotIn(
                    "PREMATURE_STATIC_PROGRESS",
                    {item["rule"] for item in corrected["findings"]},
                )
                old_pattern = deepcopy(current)
                old_pattern.pop("progress_mode", None)
                old_pattern["estimated_steps"] = old_estimate
                broken = self.validator.validate(old_pattern)
                self.assertFinding(broken, "PREMATURE_STATIC_PROGRESS", severity="ERROR")

    def test_existing_validation_exposes_structured_quality_report(self):
        result = WorkflowValidationService().validate(workflow())
        self.assertIn("quality", result)
        self.assertIn("findings", result["quality"])
        self.assertIn("metrics", result["quality"])

    def assertFinding(self, report, rule, node_id=None, severity=None):
        matches = [item for item in report["findings"] if item["rule"] == rule]
        if node_id is not None:
            matches = [item for item in matches if item["node_id"] == node_id]
        if severity is not None:
            matches = [item for item in matches if item["severity"] == severity]
        self.assertTrue(matches, f"Missing finding {rule}: {report['findings']}")


if __name__ == "__main__":
    unittest.main()
