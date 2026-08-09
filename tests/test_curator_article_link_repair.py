import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.curator_article_link_repair_service import (
    CuratorArticleLinkRepairError,
    CuratorArticleLinkRepairService,
    CuratorRepairRelationshipVerifier,
)
from app.services.curator_fix_session_service import CuratorFixSessionService
from curator.memory import CuratorMemoryStore
from curator.resolution import ResolutionPackageRepository


class CuratorArticleLinkRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "app" / "workflow_drafts").mkdir(parents=True)
        self.task_id = "GKT-ABCDEF123456"
        self.workflow_path = self.root / "app" / "workflow_drafts" / "flow.json"
        self.workflow = {
            "workflow_id": "flow", "name": "Flow", "start_node": "step",
            "nodes": {
                "step": {"type": "instruction", "title": "Inspect Output",
                         "instruction": "Inspect the selected output.", "help_text": "Original help.",
                         "next": "done"},
                "done": {"type": "resolution", "title": "Done", "message": "Done."},
            },
        }
        self._write_workflow(self.workflow)
        self.article = {
            "id": "canonical-output", "canonical_id": "canonical-output", "title": "Canonical Output",
            "review": {"status": "approved", "reviewed_by": "Reviewer", "history": ["kept"]},
        }
        KnowledgeRepository(self.root / "knowledge_base").save_published(self.article)
        memory = CuratorMemoryStore(self.root / "curation_memory")
        state = memory.load()
        state["tasks"][self.task_id] = {
            "task_id": self.task_id, "title": "Article candidate", "status": "open", "owner": "Curator",
            "priority": "Low", "classification": "Opportunity", "finding_type": "article_candidate",
            "curator_rule": "CUR-REL-ARTICLE-CANDIDATE", "future_automated_fix": True,
            "content_type": "workflow_node", "content_identifier": "flow:step", "history": [],
            "resolution_history": [], "knowledge_debt_score": 8,
        }
        memory.save(state)
        self.package = ResolutionPackageRepository(self.root / "curation_memory").save({
            "task_id": self.task_id, "recommendation": "LINK_EXISTING_ARTICLE",
            "workflow_id": "flow", "workflow_filename": "flow.json", "node_id": "step",
            "proposed_article_id": "canonical-output", "canonical_recommendation": "canonical-output",
            "identity_resolution": {"status": "matched", "canonical_article_id": "canonical-output"},
            "proposed_relationship": {"action": "RELINK_EXISTING", "workflow_id": "flow",
                                      "node_id": "step", "target_article_id": "canonical-output"},
        })
        queue = [{"item_id": "FIX-ABCDEF123456", "status": "open", "finding_type": "editorial_opportunity",
                  "knowledge_debt": 8, "affected_content": {"task_id": self.task_id}}]
        self.session = CuratorFixSessionService(self.root).create(
            started_by="Test Reviewer", originating_audit_id="audit", queue=queue,
            baseline={"counts": {}},
        )
        self.service = CuratorArticleLinkRepairService(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def _write_workflow(self, workflow):
        self.workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")

    def test_preview_is_read_only_and_complete(self):
        before = self.workflow_path.read_bytes()
        preview = self.service.preview(self.task_id)
        self.assertTrue(preview["eligible"])
        self.assertEqual(preview["before"], {"knowledge_article": None})
        self.assertEqual(preview["after"], {"knowledge_article": "canonical-output"})
        self.assertEqual(preview["node_title"], "Inspect Output")
        self.assertEqual(preview["article_publication_state"], "published")
        self.assertTrue(preview["validation"]["passed"])
        self.assertEqual(before, self.workflow_path.read_bytes())

    def test_explicit_approval_is_required(self):
        preview = self.service.preview(self.task_id)
        with self.assertRaisesRegex(CuratorArticleLinkRepairError, "Explicit reviewer approval"):
            self.service.apply(self.task_id, session_id=self.session["session_id"],
                               preview_token=preview["preview_token"], approved=False)
        self.assertNotIn("knowledge_article", json.loads(self.workflow_path.read_text())["nodes"]["step"])

    def test_apply_links_once_preserves_content_and_completes_session(self):
        article_before = (self.root / "knowledge_base" / "published" / "canonical-output.json").read_bytes()
        preview = self.service.preview(self.task_id)
        result = self.service.apply(self.task_id, session_id=self.session["session_id"],
                                    preview_token=preview["preview_token"], approved=True)
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        self.assertEqual(workflow["nodes"]["step"]["knowledge_article"], "canonical-output")
        self.assertEqual(workflow["nodes"]["step"]["instruction"], "Inspect the selected output.")
        self.assertEqual(workflow["nodes"]["step"]["help_text"], "Original help.")
        self.assertEqual(article_before, (self.root / "knowledge_base" / "published" / "canonical-output.json").read_bytes())
        task = CuratorMemoryStore(self.root / "curation_memory").load()["tasks"][self.task_id]
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(sum(event.get("event") == "deterministic_relationship_repair"
                             for event in task["history"]), 1)
        progress = CuratorFixSessionService.progress(result["session"])
        self.assertEqual(progress["session_repairs"], 1)
        self.assertEqual(progress["current_actionable"], 0)
        self.assertEqual(progress["remaining"], 0)
        with self.assertRaises(CuratorArticleLinkRepairError):
            self.service.apply(self.task_id, session_id=self.session["session_id"],
                               preview_token=preview["preview_token"], approved=True)
        refreshed = CuratorFixSessionService(self.root).get(self.session["session_id"])
        self.assertEqual(CuratorFixSessionService.progress(refreshed)["session_repairs"], 1)

    def test_already_linked_is_noop(self):
        self.workflow["nodes"]["step"]["knowledge_article"] = "canonical-output"
        self._write_workflow(self.workflow)
        preview = self.service.preview(self.task_id)
        self.assertTrue(preview["already_satisfied"])
        self.assertFalse(preview["eligible"])
        self.assertEqual(CuratorFixSessionService.progress(
            CuratorFixSessionService(self.root).get(self.session["session_id"]))["session_repairs"], 0)

    def test_different_relationship_is_never_replaced(self):
        self.workflow["nodes"]["step"]["knowledge_article"] = "other-article"
        self._write_workflow(self.workflow)
        preview = self.service.preview(self.task_id)
        self.assertFalse(preview["eligible"])
        self.assertIn("never replaces", preview["blocking_reason"])

    def test_stale_preview_blocks_without_mutation(self):
        preview = self.service.preview(self.task_id)
        self.workflow["nodes"]["step"]["help_text"] = "Concurrent edit."
        self._write_workflow(self.workflow)
        with self.assertRaisesRegex(CuratorArticleLinkRepairError, "state changed"):
            self.service.apply(self.task_id, session_id=self.session["session_id"],
                               preview_token=preview["preview_token"], approved=True)
        self.assertNotIn("knowledge_article", json.loads(self.workflow_path.read_text())["nodes"]["step"])

    def test_failed_post_write_verification_rolls_back_every_persisted_record(self):
        preview = self.service.preview(self.task_id)
        tracked = [
            self.workflow_path,
            self.root / "curation_memory" / "memory.json",
            self.root / "curation_memory" / "fix_sessions" / f"{self.session['session_id']}.json",
            self.root / "curation_memory" / "resolution_packages" / f"{self.task_id}.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        with patch.object(CuratorRepairRelationshipVerifier, "verify", return_value={"verified": False}):
            with self.assertRaisesRegex(CuratorArticleLinkRepairError, "verification failed"):
                self.service.apply(self.task_id, session_id=self.session["session_id"],
                                   preview_token=preview["preview_token"], approved=True)
        self.assertEqual(before, {path: path.read_bytes() for path in tracked})

    def test_package_relationship_snapshot_must_match_current_workflow(self):
        package_path = self.root / "curation_memory" / "resolution_packages" / f"{self.task_id}.json"
        package = json.loads(package_path.read_text())
        package["current_relationship"] = "stale-article"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        preview = self.service.preview(self.task_id)
        self.assertFalse(preview["eligible"])
        self.assertIn("no longer describes", preview["blocking_reason"])

    def test_identity_mismatch_and_unapproved_article_are_blocked(self):
        package_path = self.root / "curation_memory" / "resolution_packages" / f"{self.task_id}.json"
        package = json.loads(package_path.read_text())
        package["canonical_recommendation"] = "different"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        self.assertIn("conflicting canonical", self.service.preview(self.task_id)["blocking_reason"])
        package["canonical_recommendation"] = "canonical-output"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        article_path = self.root / "knowledge_base" / "published" / "canonical-output.json"
        article = json.loads(article_path.read_text())
        article["review"]["status"] = "pending_review"
        article_path.write_text(json.dumps(article), encoding="utf-8")
        self.assertIn("not approved", self.service.preview(self.task_id)["blocking_reason"])

    def test_missing_workflow_node_and_article_are_blocked(self):
        self.workflow_path.unlink()
        self.assertIn("workflow draft", self.service.preview(self.task_id)["blocking_reason"])
        self._write_workflow(self.workflow)
        del self.workflow["nodes"]["step"]
        self._write_workflow(self.workflow)
        self.assertIn("node", self.service.preview(self.task_id)["blocking_reason"])
        self._write_workflow({**self.workflow, "nodes": {"step": {"type": "instruction", "title": "Step",
                                                                    "instruction": "Do.", "next": "done"},
                                                          "done": {"type": "resolution", "title": "Done", "message": "Done"}}})
        (self.root / "knowledge_base" / "published" / "canonical-output.json").unlink()
        self.assertIn("not published", self.service.preview(self.task_id)["blocking_reason"])


if __name__ == "__main__":
    unittest.main()
