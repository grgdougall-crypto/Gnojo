import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_session_reconciliation_service import CuratorSessionReconciliationService
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from app.services.curator_task_service import CuratorTaskService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.workflow_publication_service import WorkflowPublicationService
from curator.checks import CuratorChecks
from curator.inventory import CuratorInventory
from curator.memory import CuratorMemoryStore
from curator.models import InventoryRecord
from curator.resolution import ResolutionPackageRepository


class CuratorLifecycleConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in ("app/decision_trees", "app/workflow_drafts", "app/workflow_publications"):
            (self.root / relative).mkdir(parents=True)
        self.article_id = "canonical-guidance"
        KnowledgeRepository(self.root / "knowledge_base").save_published({
            "id": self.article_id, "canonical_id": self.article_id,
            "title": "Canonical Guidance", "review": {"status": "approved"},
        })

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(article=None, *, long_instruction=True):
        instruction = ("Inspect the current system evidence and record every observable value before "
                       "continuing so the reviewer can compare the result with the expected state and "
                       "choose the correct evidence-based route without changing unrelated settings.")
        if not long_instruction:
            instruction = "Inspect the current state."
        step = {"type": "instruction", "title": "Inspect Evidence",
                "instruction": instruction, "next": "done"}
        if article is not None:
            step["knowledge_article"] = article
        return {"workflow_id": "flow", "name": "Flow", "category": "Networking",
                "platform": "Windows", "start_node": "step",
                "nodes": {"step": step,
                          "done": {"type": "resolution", "title": "Done", "message": "Done."}}}

    def write(self, relative, workflow):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workflow), encoding="utf-8")
        return path

    def findings(self):
        inventory = CuratorInventory(self.root).collect()
        return CuratorChecks(self.root).run(inventory)[0]

    def candidates(self):
        return [finding for finding in self.findings()
                if finding.rule == "CUR-REL-ARTICLE-CANDIDATE"]

    def save_task(self, *, persisted_articles=None):
        task_id = "GKT-LIFECYCLE"
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"][task_id] = {
            "task_id": task_id, "finding_id": "legacy-finding", "status": "open",
            "owner": "Curator", "priority": "Low", "classification": "Opportunity",
            "finding_type": "article_candidate", "title": "Article candidate",
            "content_type": "workflow_node", "content_identifier": "flow:step",
            "curator_rule": "CUR-REL-ARTICLE-CANDIDATE", "future_automated_fix": True,
            "related_content": ["flow:step"], "related_workflows": ["flow"],
            "related_articles": list(persisted_articles or []), "related_commands": [],
            "related_scripts": [], "history": [], "resolution_history": [],
            "knowledge_debt_score": 5,
        }
        store.save(state)
        return task_id

    def test_shadowed_built_in_candidate_is_suppressed_when_draft_is_satisfied(self):
        self.write("app/decision_trees/flow.json", self.workflow())
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        self.assertEqual(self.candidates(), [])

    def test_candidate_remains_when_built_in_and_draft_are_missing_relationship(self):
        self.write("app/decision_trees/flow.json", self.workflow())
        self.write("app/workflow_drafts/flow.json", self.workflow())
        candidates = self.candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provenance["lifecycle"], "draft")
        self.assertEqual(candidates[0].provenance["source_path"], "app/workflow_drafts/flow.json")
        self.assertTrue(candidates[0].provenance["content_fingerprint"])

    def test_unresolved_draft_relationship_requires_human_review(self):
        self.write("app/workflow_drafts/flow.json", self.workflow("missing-article"))
        task_id = self.save_task()
        before = (self.root / "app/workflow_drafts/flow.json").read_bytes()
        result = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(result["status"], "relationship_conflict_or_unresolved")
        self.assertTrue(result["human_approval_required"])
        self.assertEqual(before, (self.root / "app/workflow_drafts/flow.json").read_bytes())
        self.assertEqual(CuratorMemoryStore(self.root / "curation_memory").load()["tasks"][task_id]["status"], "open")

    def test_different_valid_relationship_requires_review_when_package_identity_differs(self):
        KnowledgeRepository(self.root / "knowledge_base").save_published({
            "id": "different-guidance", "canonical_id": "different-guidance",
            "title": "Different Guidance", "review": {"status": "approved"},
        })
        self.write("app/workflow_drafts/flow.json", self.workflow("different-guidance"))
        task_id = self.save_task()
        ResolutionPackageRepository(self.root / "curation_memory").save({
            "task_id": task_id, "canonical_recommendation": self.article_id,
            "proposed_article_id": self.article_id,
            "identity_resolution": {"canonical_article_id": self.article_id},
        })
        result = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(result["status"], "relationship_conflict_or_unresolved")
        self.assertEqual(result["expected_canonical_article_id"], self.article_id)
        self.assertEqual(CuratorMemoryStore(self.root / "curation_memory").load()["tasks"][task_id]["status"], "open")

    def test_target_unavailable_is_conservative(self):
        task_id = self.save_task()
        result = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(result["status"], "target_unavailable")
        self.assertTrue(result["human_approval_required"])
        self.assertEqual(CuratorMemoryStore(self.root / "curation_memory").load()["tasks"][task_id]["status"], "open")

    def test_published_successor_suppresses_built_in_candidate(self):
        self.write("app/decision_trees/flow.json", self.workflow())
        WorkflowPublicationService(self.root / "app/workflow_publications").publish(
            self.workflow(self.article_id), "flow.json")
        self.assertEqual(self.candidates(), [])
        target = CuratorWorkflowLifecycleService(self.root).resolve("flow")
        self.assertEqual(target.lifecycle, "published")

    def test_deterministic_defects_in_shadowed_built_in_remain_auditable(self):
        broken = self.workflow()
        broken["nodes"]["step"]["next"] = "missing"
        self.write("app/decision_trees/flow.json", broken)
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        defects = [finding for finding in self.findings()
                   if finding.rule == "GNOJO-WORKFLOW-VALIDATOR"]
        self.assertTrue(defects)
        self.assertEqual(defects[0].provenance["lifecycle"], "built_in")

    def test_targeted_verification_returns_missing_without_mutation(self):
        self.write("app/workflow_drafts/flow.json", self.workflow())
        task_id = self.save_task()
        before = (self.root / "app/workflow_drafts/flow.json").read_bytes()
        result = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(result["status"], "relationship_missing")
        self.assertTrue(result["human_approval_required"])
        self.assertEqual(before, (self.root / "app/workflow_drafts/flow.json").read_bytes())

    def test_satisfied_verification_reconciles_without_repair_and_is_idempotent(self):
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        task_id = self.save_task()
        service = CuratorTargetedVerificationService(self.root)
        before = (self.root / "app/workflow_drafts/flow.json").read_bytes()
        first = service.verify(task_id)
        second = service.verify(task_id)
        task = CuratorMemoryStore(self.root / "curation_memory").load()["tasks"][task_id]
        self.assertEqual(first["status"], "relationship_satisfied")
        self.assertEqual(second["status"], "relationship_satisfied")
        self.assertTrue(first["no_action_required"])
        self.assertEqual(task["status"], "resolved")
        self.assertFalse(task["resolution_metadata"]["repair_performed"])
        self.assertEqual(sum(event.get("event") == "relationship_satisfied_no_action_required"
                             for event in task["history"]), 1)
        self.assertEqual(before, (self.root / "app/workflow_drafts/flow.json").read_bytes())

    def test_related_knowledge_uses_live_truth_not_historical_array(self):
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        task_id = self.save_task(persisted_articles=["historical-article"])
        task = CuratorTaskService(self.root).get(task_id)
        self.assertEqual(task["live_related_knowledge"]["articles"][0]["id"], self.article_id)
        self.assertEqual(task["live_related_knowledge"]["articles"][0]["title"], "Canonical Guidance")
        self.assertEqual(task["live_related_knowledge"]["lifecycle"], "draft")
        self.assertEqual(task["related_articles"], ["historical-article"])

    def test_legacy_task_without_provenance_loads_safely(self):
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        task = CuratorTaskService(self.root).get(self.save_task())
        self.assertNotIn("provenance", task)
        self.assertEqual(task["current_content"]["lifecycle"], "draft")

    def test_fix_wizard_marks_satisfaction_external_without_counting_repair(self):
        self.write("app/workflow_drafts/flow.json", self.workflow(self.article_id))
        task_id = self.save_task()
        item = {"item_id": "FIX-LIFECYCLE", "status": "open", "knowledge_debt": 5,
                "finding_type": "editorial_opportunity",
                "affected_content": {"task_id": task_id}}
        sessions = CuratorFixSessionService(self.root)
        session = sessions.create(started_by="Reviewer", originating_audit_id="AUD-1",
                                  queue=[item], baseline={"counts": {}})
        CuratorTargetedVerificationService(self.root).verify(task_id)
        reconciler = CuratorSessionReconciliationService(self.root)
        reconciler.integrity.report = Mock(return_value={"counts": {}})
        reconciler.planner.build = Mock(return_value=[])
        first = reconciler.reconcile(session["session_id"], trigger="targeted_verification")
        second = reconciler.reconcile(session["session_id"], trigger="refresh")
        first_progress = sessions.progress(first)
        second_progress = sessions.progress(second)
        self.assertEqual(first["repair_queue"][0]["status"], "resolved_external")
        self.assertEqual(first_progress["current_actionable"], 0)
        self.assertEqual(first_progress["remaining"], 0)
        self.assertEqual(first_progress["session_repairs"], 0)
        self.assertEqual(second_progress, first_progress)


if __name__ == "__main__":
    unittest.main()
