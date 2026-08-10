import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from curator.calibration import ReasoningCalibrationService
from curator.learning import CuratorLearningService
from curator.memory import CuratorMemoryStore
from curator.models import AuditFilter
from curator.tasks import KnowledgeTaskService
from curator.workflow_reasoning import WorkflowReasoningAuditor


def reasoning_task(task_id="GKT-A", workflow="printer", node="check_power", *,
                   disposition="NOT_REVIEWED", status="open", distance=2,
                   rule="CUR-WR-EARLY-CONVERGENCE"):
    return {
        "task_id": task_id, "finding_id": f"finding-{workflow}-{node}",
        "durable_identity": f"durable-{workflow}-{node}", "curator_rule": rule,
        "classification": "Opportunity", "status": status, "priority": "Low",
        "owner": "Workflow Designer", "knowledge_debt_score": 5.0,
        "content_type": "workflow_node", "content_identifier": f"{workflow}:{node}",
        "related_workflows": [workflow], "review_disposition": disposition,
        "history": [], "evidence": [
            "Branches converge.",
            ("Structural evidence: {'branch_labels': ['Yes', 'No'], "
             f"'destinations': ['left', 'right'], 'convergence_node': 'join', 'distance': {distance}}}"),
        ],
    }


class ReasoningCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.service = ReasoningCalibrationService()

    def test_equivalent_structures_share_fingerprint(self):
        first = reasoning_task("A", "printer", "power")
        second = reasoning_task("B", "vpn", "credentials")
        self.assertEqual(self.service.current_snapshot(first)["structural_fingerprint"],
                         self.service.current_snapshot(second)["structural_fingerprint"])

    def test_materially_different_structures_have_different_fingerprints(self):
        self.assertNotEqual(
            self.service.current_snapshot(reasoning_task(distance=1))["structural_fingerprint"],
            self.service.current_snapshot(reasoning_task(distance=3))["structural_fingerprint"])

    def test_workflow_name_does_not_define_pattern_identity(self):
        a = reasoning_task(workflow="Friendly Printer Name")
        b = reasoning_task(workflow="Renamed Printer")
        self.assertEqual(self.service.current_snapshot(a)["structural_fingerprint"],
                         self.service.current_snapshot(b)["structural_fingerprint"])

    def test_task_id_does_not_define_pattern_identity(self):
        self.assertEqual(self.service.current_snapshot(reasoning_task("A"))["structural_fingerprint"],
                         self.service.current_snapshot(reasoning_task("Z"))["structural_fingerprint"])

    def test_snapshot_captures_lifecycle_context(self):
        snapshot = self.service.snapshot(reasoning_task(status="resolved"), "INTENTIONAL",
                                         reviewed_at="2026-08-09T00:00:00+00:00")
        self.assertEqual((snapshot["workflow_id"], snapshot["node_id"],
                          snapshot["finding_status_at_review"]), ("printer", "check_power", "resolved"))

    def test_summary_totals_are_consistent(self):
        tasks = [reasoning_task("A", disposition="USEFUL"),
                 reasoning_task("B", disposition="INTENTIONAL"), reasoning_task("C")]
        summary = self.service.summary(tasks)
        self.assertEqual((summary["total"], summary["reviewed"], summary["NOT_REVIEWED"]), (3, 2, 1))

    def test_resolved_reviews_are_included_in_summary(self):
        summary = self.service.summary([reasoning_task(disposition="INTENTIONAL", status="resolved")])
        self.assertEqual(summary["historical_resolved_reviewed"], 1)

    def test_summary_breaks_down_rule_workflow_and_pattern(self):
        summary = self.service.summary([reasoning_task(disposition="USEFUL")])
        self.assertEqual(summary["by_rule"][0]["reviewed"], 1)
        self.assertEqual(summary["by_workflow"][0]["key"], "printer")
        self.assertEqual(summary["by_pattern"][0]["reviewed"], 1)

    def test_mixed_pattern_is_reported_not_resolved(self):
        summary = self.service.summary([
            reasoning_task("A", disposition="USEFUL"),
            reasoning_task("B", disposition="INTENTIONAL")])
        self.assertEqual(summary["mixed_pattern_count"], 1)

    def test_context_excludes_current_review(self):
        current = reasoning_task("A", disposition="USEFUL")
        context = self.service.context(current, [current, reasoning_task("B", disposition="INTENTIONAL")])
        self.assertEqual((context["prior_review_count"], context["dispositions"]),
                         (1, {"INTENTIONAL": 1}))

    def test_context_is_explicitly_advisory(self):
        context = self.service.context(reasoning_task(), [])
        self.assertIn("No automatic decision", context["advisory"])

    def test_repeated_evidence_creates_a_lesson(self):
        tasks = [reasoning_task("A", disposition="USEFUL"),
                 reasoning_task("B", disposition="USEFUL")]
        lessons = self.service.recurring_lessons(tasks)
        self.assertEqual((len(lessons), len(lessons[0]["evidence_task_ids"])), (1, 2))

    def test_single_review_does_not_create_a_lesson(self):
        self.assertEqual(self.service.recurring_lessons([reasoning_task(disposition="USEFUL")]), [])

    def test_lessons_distinguish_useful_and_intentional(self):
        tasks = [reasoning_task("A", disposition="USEFUL"), reasoning_task("B", disposition="USEFUL"),
                 reasoning_task("C", disposition="INTENTIONAL"), reasoning_task("D", disposition="INTENTIONAL")]
        patterns = [lesson["pattern"] for lesson in self.service.recurring_lessons(tasks)]
        self.assertTrue(any("USEFUL" in item for item in patterns))
        self.assertTrue(any("INTENTIONAL" in item for item in patterns))

    def test_false_positive_is_kept_separate(self):
        tasks = [reasoning_task("A", disposition="FALSE_POSITIVE"),
                 reasoning_task("B", disposition="FALSE_POSITIVE")]
        lesson = self.service.recurring_lessons(tasks)[0]
        self.assertIn("FALSE_POSITIVE", lesson["pattern"])

    def test_learning_service_includes_advisory_calibration(self):
        tasks = {item["task_id"]: item for item in [
            reasoning_task("A", disposition="USEFUL"), reasoning_task("B", disposition="USEFUL")]}
        analysis = CuratorLearningService().analyze(tasks)
        self.assertTrue(analysis["reasoning_calibration"]["advisory_only"])
        self.assertTrue(any(item["pattern"].startswith("reasoning_calibration:")
                            for item in analysis["lessons"]))

    def test_analysis_does_not_mutate_tasks(self):
        tasks = {"A": reasoning_task("A", disposition="USEFUL")}; before = deepcopy(tasks)
        CuratorLearningService().analyze(tasks)
        self.assertEqual(tasks, before)

    @staticmethod
    def _reasoning_workflow(*, converges=True):
        right_target = "shared" if converges else "right_done"
        return {
            "workflow_id": "calibration-fixture", "name": "Calibration Fixture",
            "description": "Fixture", "category": "Networking", "platform": "Windows",
            "estimated_steps": 3, "start_node": "q", "nodes": {
                "q": {"type": "question", "question": "Which path?", "answers": {
                    "a": {"label": "A", "next": "left"},
                    "b": {"label": "B", "next": "right"}}},
                "left": {"type": "instruction", "title": "Inspect A", "next": "shared"},
                "right": {"type": "instruction", "title": "Inspect B", "next": right_target},
                "shared": {"type": "resolution", "title": "Shared result"},
                "right_done": {"type": "resolution", "title": "Separate result"},
            },
        }

    def test_human_review_does_not_suppress_future_findings(self):
        workflow = self._reasoning_workflow(converges=True)
        auditor = WorkflowReasoningAuditor()
        before = [item.rule for item in auditor.analyze(workflow)]
        self.service.summary([reasoning_task(disposition="INTENTIONAL")])
        after = [item.rule for item in auditor.analyze(workflow)]
        self.assertEqual(before, after)
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", after)

    def test_human_review_does_not_force_future_findings(self):
        workflow = self._reasoning_workflow(converges=False)
        auditor = WorkflowReasoningAuditor()
        self.service.summary([reasoning_task(disposition="USEFUL")])
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE",
                         [item.rule for item in auditor.analyze(workflow)])


class ReasoningCalibrationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = CuratorMemoryStore(self.root); state = self.store.load()
        state["tasks"] = {"GKT-A": reasoning_task()}; self.store.save(state)

    def tearDown(self): self.temp.cleanup()

    def test_review_snapshot_persists_after_reload(self):
        self.store.update_review_disposition("GKT-A", "USEFUL")
        task = CuratorMemoryStore(self.root).load()["tasks"]["GKT-A"]
        self.assertEqual(task["reasoning_calibration"]["disposition"], "USEFUL")

    def test_review_event_contains_traceable_snapshot(self):
        task = self.store.update_review_disposition("GKT-A", "INTENTIONAL")
        event = task["history"][-1]
        self.assertEqual(event["calibration"]["finding_identity"], task["finding_id"])

    def test_repeated_review_is_idempotent(self):
        first = self.store.update_review_disposition("GKT-A", "USEFUL")
        second = self.store.update_review_disposition("GKT-A", "USEFUL")
        self.assertEqual(len(first["history"]), len(second["history"]))

    def test_disposition_does_not_change_status_priority_or_debt(self):
        before = self.store.load()["tasks"]["GKT-A"]
        after = self.store.update_review_disposition("GKT-A", "FALSE_POSITIVE")
        self.assertEqual((after["status"], after["priority"], after["knowledge_debt_score"]),
                         (before["status"], before["priority"], before["knowledge_debt_score"]))

    def test_disposition_does_not_change_audit_health_state(self):
        state = self.store.load(); state["audits"] = [{"run_id": "RUN-1", "health": 91.5}]
        self.store.save(state); self.store.update_review_disposition("GKT-A", "USEFUL")
        self.assertEqual(self.store.load()["audits"], state["audits"])

    def test_reconciliation_preserves_calibration(self):
        self.store.update_review_disposition("GKT-A", "USEFUL"); state = self.store.load()
        before = deepcopy(state["tasks"]["GKT-A"]["reasoning_calibration"])
        KnowledgeTaskService().reconcile(state, [], [], run_id="RUN-2",
                                         observed_at="2026-08-09T01:00:00+00:00",
                                         filters=AuditFilter())
        self.assertEqual(state["tasks"]["GKT-A"]["reasoning_calibration"], before)

    def test_missing_finding_does_not_erase_calibration_evidence(self):
        self.store.update_review_disposition("GKT-A", "INTENTIONAL"); state = self.store.load()
        KnowledgeTaskService().reconcile(state, [], [], run_id="RUN-2",
                                         observed_at="2026-08-09T01:00:00+00:00",
                                         filters=AuditFilter(content_type="article"))
        self.assertIn("reasoning_calibration", state["tasks"]["GKT-A"])

    def test_reappearance_uses_stable_task_identity_and_review(self):
        self.store.update_review_disposition("GKT-A", "USEFUL"); state = self.store.load()
        task_before = deepcopy(state["tasks"]["GKT-A"])
        KnowledgeTaskService().reconcile(state, [], [], run_id="RUN-3",
                                         observed_at="2026-08-09T02:00:00+00:00",
                                         filters=AuditFilter(content_type="article"))
        task_after = state["tasks"]["GKT-A"]
        self.assertEqual((task_after["task_id"], task_after["review_disposition"]),
                         (task_before["task_id"], "USEFUL"))


if __name__ == "__main__":
    unittest.main()
