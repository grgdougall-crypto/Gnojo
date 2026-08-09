import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.repositories.knowledge_repository import ArticleAlreadyExistsError, KnowledgeRepository
from app.services.knowledge_integrity_service import KnowledgeIntegrityError, KnowledgeIntegrityService
from app.services.knowledge_publication_service import KnowledgePublicationError, KnowledgePublicationService
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.workflow_draft_service import WorkflowDraftService
from curator.inventory import CuratorInventory


class KnowledgeIntegrityEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = KnowledgeRepository(self.root / "knowledge_base")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def article(article_id, title="Article"):
        return {"id": article_id, "title": title, "category": "Test", "overview": "Test article."}

    def test_published_canonical_identity_is_unique(self):
        self.repository.save_published(self.article("one"))
        duplicate = self.article("two")
        duplicate["canonical_id"] = "one"
        with self.assertRaises(ArticleAlreadyExistsError):
            self.repository.save_published(duplicate)

    def test_publication_stamps_provenance_updates_relationship_and_reindexes(self):
        workflow_directory = self.root / "app" / "workflow_drafts"
        workflow_directory.parent.mkdir(parents=True, exist_ok=True)
        workflow_service = WorkflowDraftService(workflow_directory)
        workflow_service.save_draft({"workflow_id": "test", "nodes": {"step": {"type": "instruction"}}})
        article = self.article("linked")
        article["workflow_origin"] = {"filename": "test.json", "node_id": "step"}
        self.repository.save_draft(article)

        published = KnowledgePublicationService(self.repository, workflow_service).publish("linked", reviewer="Reviewer")

        self.assertEqual(published["canonical_id"], "linked")
        self.assertEqual(published["review"]["reviewed_by"], "Reviewer")
        self.assertTrue(published["review"]["reviewed_at"])
        self.assertFalse((self.repository.draft_directory / "linked.json").exists())
        workflow = json.loads((workflow_directory / "test.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow["nodes"]["step"]["knowledge_article"], "linked")
        self.assertTrue((self.root / "knowledge_base" / "inventory.json").exists())

    def test_publication_rolls_back_when_relationship_update_fails(self):
        (self.root / "app").mkdir(parents=True, exist_ok=True)
        workflow_service = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        article = self.article("rollback")
        article["workflow_origin"] = {"filename": "missing.json", "node_id": "step"}
        self.repository.save_draft(article)
        with self.assertRaises(KnowledgePublicationError):
            KnowledgePublicationService(self.repository, workflow_service).publish("rollback")
        self.assertTrue((self.repository.draft_directory / "rollback.json").exists())
        self.assertFalse((self.repository.published_directory / "rollback.json").exists())

    def test_inventory_preserves_real_source_path_for_canonical_alias(self):
        path = self.repository.published_directory / "legacy-file.json"
        value = self.article("legacy-file")
        value["canonical_id"] = "canonical"
        path.write_text(json.dumps(value), encoding="utf-8")
        record = next(item for item in CuratorInventory(self.root).collect() if item.content_type == "article")
        self.assertEqual(record.identifier, "canonical")
        self.assertTrue(record.source_path.endswith("legacy-file.json"))

    def test_merge_updates_draft_and_published_workflows_and_archives_duplicates(self):
        self.repository.save_published(self.article("canonical", "Shared"))
        self.repository.save_published(self.article("duplicate", "Shared"))
        for directory, wrapped in (("workflow_drafts", False), ("workflow_publications", True)):
            path = self.root / "app" / directory / "flow.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            workflow = {"workflow_id": "flow", "nodes": {"step": {"knowledge_article": "duplicate"}}}
            path.write_text(json.dumps({"workflow": workflow} if wrapped else workflow), encoding="utf-8")

        result = KnowledgeIntegrityService(self.root).merge("canonical", ["duplicate"])

        self.assertEqual(result["canonical_id"], "canonical")
        self.assertFalse((self.repository.published_directory / "duplicate.json").exists())
        self.assertTrue((self.repository.archive_directory / "duplicate.json").exists())
        for directory in ("workflow_drafts", "workflow_publications"):
            document = json.loads((self.root / "app" / directory / "flow.json").read_text(encoding="utf-8"))
            workflow = document.get("workflow", document)
            self.assertEqual(workflow["nodes"]["step"]["knowledge_article"], "canonical")
        self.assertEqual(ArticleIdentityResolver(self.repository).aliases()["duplicate"], "canonical")
        self.assertEqual(self.repository.resolve_published_article("duplicate")["id"], "canonical")

    def test_normalized_title_prevents_numbered_duplicate(self):
        self.repository.save_published(self.article("using-device-manager-to-troubleshoot-hardware", "Using Device Manager to Troubleshoot Hardware"))
        match = ArticleIdentityResolver(self.repository).resolve(candidate={"title": " using device manager TO troubleshoot hardware "})
        self.assertIsNotNone(match)
        self.assertEqual(match.article["id"], "using-device-manager-to-troubleshoot-hardware")
        self.assertEqual(match.method, "normalized_title")

    def test_delete_restrictions_explain_canonical_and_state(self):
        self.repository.save_published(self.article("canonical"))
        policy = KnowledgeIntegrityService(self.root).lifecycle_policy("canonical")
        self.assertTrue(policy["can_archive"])
        self.assertFalse(policy["can_soft_delete"])
        self.assertIn("The article must be archived first.", policy["soft_delete_reasons"])

    def test_merge_preview_does_not_mutate_content(self):
        canonical = self.article("canonical", "Shared"); canonical["tags"] = ["one"]
        duplicate = self.article("duplicate", "Shared"); duplicate["tags"] = ["two"]
        self.repository.save_published(canonical); self.repository.save_published(duplicate)
        before = (self.repository.published_directory / "canonical.json").read_bytes()
        preview = KnowledgeIntegrityService(self.root).merge_preview("canonical", ["duplicate"])
        self.assertEqual(preview["additions"]["tags"], ["two"])
        self.assertEqual(before, (self.repository.published_directory / "canonical.json").read_bytes())

    def test_merge_preview_deduplicates_additions_from_multiple_records(self):
        canonical = self.article("canonical", "Shared")
        duplicate_one = self.article("duplicate-one", "Shared"); duplicate_one["checklist"] = ["Shared step"]
        duplicate_two = self.article("duplicate-two", "Shared"); duplicate_two["checklist"] = ["Shared step"]
        self.repository.save_published(canonical)
        self.repository.save_published(duplicate_one)
        self.repository.save_published(duplicate_two)

        preview = KnowledgeIntegrityService(self.root).merge_preview(
            "canonical", ["duplicate-one", "duplicate-two"]
        )

        self.assertEqual(preview["additions"]["checklist"], ["Shared step"])

    def test_merge_rolls_back_every_store_on_failure(self):
        self.repository.save_published(self.article("canonical", "Shared"))
        self.repository.save_published(self.article("duplicate", "Shared"))
        service = KnowledgeIntegrityService(self.root)
        with patch.object(service.repository, "archive_article", side_effect=OSError("failure")):
            with self.assertRaises(KnowledgeIntegrityError):
                service.merge("canonical", ["duplicate"])
        self.assertTrue((self.repository.published_directory / "duplicate.json").exists())
        self.assertFalse((self.repository.archive_directory / "duplicate.json").exists())


if __name__ == "__main__":
    unittest.main()
