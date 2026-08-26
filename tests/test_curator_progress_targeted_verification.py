import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.services.curator_targeted_verification_service import (
    CuratorTargetedVerificationService,
)
from curator.memory import CuratorMemoryStore


class CuratorProgressTargetedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(*, branch_aware=False):
        nodes = {}
        for step in range(1, 6):
            destination = f"step_{step + 1}" if step < 5 else "done"
            nodes[f"step_{step}"] = {
                "type": "question",
                "question": f"Check {step}?",
                "answers": {"yes": {"label": "Yes", "next": destination}},
            }
        nodes["done"] = {"type": "resolution", "title": "Done"}
        result = {
            "workflow_id": "higher_layer_connectivity",
            "name": "Higher-Layer Connectivity Diagnostics",
            "category": "Networking",
            "platform": "Cross-platform",
            "estimated_steps": 4,
            "start_node": "step_1",
            "nodes": nodes,
        }
        if branch_aware:
            result["progress_mode"] = "branch_aware"
        return result

    @staticmethod
    def task():
        return {
            "task_id": "GKT-PROGRESS",
            "finding_id": "CUR-PROGRESS",
            "status": "open",
            "owner": "Unassigned",
            "priority": "Medium",
            "classification": "Opportunity",
            "finding_type": "workflow_reasoning_progress_inconsistency",
            "content_type": "workflow",
            "content_identifier": "higher_layer_connectivity",
            "curator_rule": "CUR-WR-PROGRESS",
            "evidence": ["Configured estimated steps: 4", "Longest user-visible path: 6"],
            "structured_evidence": {
                "configured_steps": 4,
                "maximum_user_visible_nodes": 6,
            },
            "history": [],
            "resolution_history": [],
        }

    def save_task_and_workflow(self, workflow=None):
        state = self.store.load()
        state["tasks"] = {"GKT-PROGRESS": self.task()}
        self.store.save(state)
        if workflow is not None:
            drafts = self.root / "app" / "workflow_drafts"
            drafts.mkdir(parents=True)
            (drafts / "higher_layer_connectivity.json").write_text(
                json.dumps(workflow), encoding="utf-8",
            )

    def test_current_four_of_six_defect_is_still_detected_read_only(self):
        self.save_task_and_workflow(self.workflow())
        workflow_path = self.root / "app/workflow_drafts/higher_layer_connectivity.json"
        workflow_before = workflow_path.read_bytes()
        task_before = copy.deepcopy(self.store.load()["tasks"]["GKT-PROGRESS"])

        result = CuratorTargetedVerificationService(self.root).verify("GKT-PROGRESS")
        task_after = self.store.load()["tasks"]["GKT-PROGRESS"]

        self.assertEqual(result["status"], "still_detected")
        self.assertEqual(workflow_path.read_bytes(), workflow_before)
        for field in ("task_id", "finding_id", "status", "classification",
                      "content_type", "content_identifier", "curator_rule"):
            self.assertEqual(task_after[field], task_before[field])
        self.assertEqual(task_after["evidence"], task_before["evidence"])

    def test_branch_aware_current_workflow_appears_corrected(self):
        self.save_task_and_workflow(self.workflow(branch_aware=True))

        result = CuratorTargetedVerificationService(self.root).verify("GKT-PROGRESS")

        self.assertEqual(result["status"], "appears_corrected")
        self.assertIn("no longer detects", result["message"])
        self.assertEqual(self.store.load()["tasks"]["GKT-PROGRESS"]["status"], "open")

    def test_unresolvable_authoritative_workflow_requires_human_review(self):
        self.save_task_and_workflow()

        result = CuratorTargetedVerificationService(self.root).verify("GKT-PROGRESS")

        self.assertEqual(result["status"], "human_review_required")
        self.assertIn("authoritative affected workflow", result["message"])
        self.assertEqual(self.store.load()["tasks"]["GKT-PROGRESS"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
