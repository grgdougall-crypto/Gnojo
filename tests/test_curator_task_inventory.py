import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.services.curator_task_inventory_service import CuratorTaskInventoryService
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.models import AuditFilter
from curator.tasks import KnowledgeTaskService


def task(task_id, rule="CUR-WR-PROGRESS", workflow="printer", **values):
    item = {
        "task_id": task_id, "finding_id": f"finding-{task_id}", "curator_rule": rule,
        "classification": "Risk", "status": "open", "title": f"Task {task_id}",
        "content_type": "workflow_node", "content_identifier": f"{workflow}:node-{task_id}",
        "related_workflows": [workflow], "priority": "Medium", "history": [],
    }
    item.update(values)
    return item


class CuratorTaskInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = CuratorTaskInventoryService(self.root)
        self.tasks = [
            task("A", title="Progress finding"),
            task("B", rule="CUR-WR-SIGNAL-RETENTION", workflow="internet", status="resolved",
                 classification="Opportunity", review_disposition="USEFUL"),
            task("C", rule="CUR-REL-ARTICLE-CANDIDATE", workflow="printer",
                 classification="Defect"),
        ]

    def tearDown(self): self.temp.cleanup()

    def visible(self, **filters): return self.service.filter(self.tasks, filters)["tasks"]

    def test_empty_filters_preserve_all_tasks(self): self.assertEqual(len(self.visible()), 3)
    def test_status_filter(self): self.assertEqual([x["task_id"] for x in self.visible(status="resolved")], ["B"])
    def test_classification_filter(self): self.assertEqual([x["task_id"] for x in self.visible(classification="Defect")], ["C"])
    def test_workflow_filter(self): self.assertEqual({x["task_id"] for x in self.visible(workflow="printer")}, {"A", "C"})
    def test_reasoning_family_filter(self): self.assertEqual({x["task_id"] for x in self.visible(family="workflow_reasoning")}, {"A", "B"})
    def test_other_family_filter(self): self.assertEqual([x["task_id"] for x in self.visible(family="other")], ["C"])
    def test_reasoning_rule_filter(self): self.assertEqual([x["task_id"] for x in self.visible(rule="CUR-WR-PROGRESS")], ["A"])
    def test_title_search(self): self.assertEqual([x["task_id"] for x in self.visible(q="progress")], ["A"])
    def test_rule_search(self): self.assertEqual([x["task_id"] for x in self.visible(q="signal-retention")], ["B"])
    def test_finding_search(self): self.assertEqual([x["task_id"] for x in self.visible(q="finding-c")], ["C"])
    def test_node_search(self): self.assertEqual([x["task_id"] for x in self.visible(q="node-b")], ["B"])
    def test_filters_compose(self): self.assertEqual([x["task_id"] for x in self.visible(family="workflow_reasoning", status="open")], ["A"])
    def test_disposition_filter(self): self.assertEqual([x["task_id"] for x in self.visible(disposition="USEFUL")], ["B"])
    def test_default_disposition_is_backward_compatible(self): self.assertEqual(self.visible()[0]["review_disposition"], "NOT_REVIEWED")
    def test_human_rule_label_is_derived(self): self.assertEqual(self.visible()[0]["rule_label"], "Progress Inconsistency")
    def test_summary_counts_reviewed_tasks(self):
        summary = self.service.filter(self.tasks, {"family": "workflow_reasoning"})["calibration"]
        self.assertEqual((summary["total"], summary["reviewed"], summary["USEFUL"]), (2, 1, 1))
    def test_summary_is_shown_only_for_reasoning_family(self):
        self.assertTrue(self.service.filter(self.tasks, {"family": "workflow_reasoning"})["show_calibration"])
        self.assertFalse(self.service.filter(self.tasks, {})["show_calibration"])
    def test_options_include_only_reasoning_rules(self):
        rules = dict(self.service.filter(self.tasks, {})["options"]["rules"])
        self.assertIn("CUR-WR-PROGRESS", rules); self.assertNotIn("CUR-REL-ARTICLE-CANDIDATE", rules)
    def test_rule_options_follow_canonical_auditor_order(self):
        expected = ["CUR-WR-SIGNAL-RETENTION", "CUR-WR-PROGRESS"]
        actual = [value for value, _ in self.service.filter(self.tasks, {})["options"]["rules"]]
        self.assertEqual(actual, expected)
    def test_option_collections_are_unique(self):
        options = self.service.filter(self.tasks + [deepcopy(self.tasks[0])], {})["options"]
        for name in ("statuses", "classifications", "workflows", "rules", "dispositions"):
            self.assertEqual(len(options[name]), len(set(options[name])))


class CuratorReviewDispositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = CuratorMemoryStore(self.root); state = self.store.load()
        state["tasks"] = {"GKT-A": task("GKT-A"), "GKT-C": task("GKT-C", rule="CUR-OTHER")}
        self.store.save(state)

    def tearDown(self): self.temp.cleanup()

    def test_disposition_persists_after_reload(self):
        self.store.update_review_disposition("GKT-A", "USEFUL")
        self.assertEqual(CuratorMemoryStore(self.root).load()["tasks"]["GKT-A"]["review_disposition"], "USEFUL")
    def test_disposition_does_not_change_lifecycle(self):
        before = deepcopy(self.store.load()["tasks"]["GKT-A"])
        after = self.store.update_review_disposition("GKT-A", "INTENTIONAL")
        self.assertEqual((after["status"], after["content_identifier"]), (before["status"], before["content_identifier"]))
    def test_false_positive_is_calibration_not_resolution(self):
        after = self.store.update_review_disposition("GKT-A", "FALSE_POSITIVE")
        self.assertEqual(after["status"], "open")
    def test_invalid_disposition_is_rejected(self):
        with self.assertRaises(CuratorMemoryError): self.store.update_review_disposition("GKT-A", "RESOLVED")
    def test_non_reasoning_task_is_unchanged(self):
        before = self.store.load()["tasks"]["GKT-C"]
        with self.assertRaises(CuratorMemoryError): self.store.update_review_disposition("GKT-C", "USEFUL")
        self.assertEqual(self.store.load()["tasks"]["GKT-C"], before)
    def test_reconciliation_preserves_disposition(self):
        self.store.update_review_disposition("GKT-A", "USEFUL")
        state = self.store.load(); KnowledgeTaskService().reconcile(
            state, [], [], run_id="RUN-2", observed_at="2026-08-09T00:00:00+00:00",
            filters=AuditFilter())
        self.assertEqual(state["tasks"]["GKT-A"]["review_disposition"], "USEFUL")


if __name__ == "__main__": unittest.main()
