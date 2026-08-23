import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.repositories.knowledge_repository import ArticleNotFoundError, KnowledgeRepository
from app.services.assisted_resolution_validator import AssistedResolutionValidator
from app.services.curator_batch_service import CuratorBatchService
from app.services.curator_resolution_service import CuratorResolutionService
from app.services.knowledge_publication_service import KnowledgePublicationService
from app.services.workflow_draft_service import WorkflowDraftService
from app.app import app as flask_app
from curator.memory import CuratorMemoryStore
from curator.resolution import ResolutionPackageError, ResolutionPackageRepository


class CuratorAssistedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "app" / "workflow_drafts").mkdir(parents=True)
        workflow = {
            "workflow_id": "test_workflow", "name": "Test Workflow", "category": "Networking", "platform": "Windows",
            "nodes": {
                "step_one": {"type": "instruction", "title": "Inspect the Adapter", "instruction": "Open the approved network settings and record the active adapter status.", "next": "done"},
                "done": {"type": "resolution", "title": "Done", "message": "Complete."},
            },
        }
        (self.root / "app" / "workflow_drafts" / "test_workflow.json").write_text(json.dumps(workflow), encoding="utf-8")
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        for index in range(10):
            task_id = f"GKT-TEST{index}"
            state["tasks"][task_id] = {
                "task_id": task_id, "finding_id": f"CUR-{index}", "finding_type": "article_candidate",
                "classification": "Opportunity", "status": "open", "content_identifier": "test_workflow:step_one",
                "confidence": "high", "evidence": [workflow["nodes"]["step_one"]["instruction"]],
            }
        store.save(state)

    def tearDown(self):
        self.temporary.cleanup()

    def test_package_is_persistent_versioned_and_complete(self):
        service = CuratorResolutionService(self.root)
        first = service.prepare("GKT-TEST0")
        second = service.prepare("GKT-TEST0")
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["recommendation"], "CREATE_NEW_ARTICLE")
        self.assertEqual(second["validation_status"], "passed")
        self.assertEqual(len(second["history"]), 2)

    def test_draft_creation_requires_confirmation_and_never_changes_workflow(self):
        service = CuratorResolutionService(self.root)
        package = service.prepare("GKT-TEST0")
        before = (self.root / "app" / "workflow_drafts" / "test_workflow.json").read_text(encoding="utf-8")
        with self.assertRaises(ResolutionPackageError):
            service.create_article_draft("GKT-TEST0", confirmed=False)
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        after = (self.root / "app" / "workflow_drafts" / "test_workflow.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(article["review"]["status"], "draft")
        self.assertEqual(article["workflow_origin"]["curator_task_id"], "GKT-TEST0")
        self.assertTrue(article["tags"])
        self.assertEqual(package["proposed_article_id"], article["id"])

    def test_creation_is_idempotent_and_package_points_to_the_same_draft(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        first = service.create_article_draft("GKT-TEST0", confirmed=True)
        second = service.create_article_draft("GKT-TEST0", confirmed=True)
        package = service.get("GKT-TEST0")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(package["draft_article_id"], first["id"])
        self.assertEqual(len(KnowledgeRepository(self.root / "knowledge_base").get_drafts()), 1)

    def test_package_refresh_retains_durable_article_pointer(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)

        refreshed = service.prepare("GKT-TEST0")

        self.assertEqual(refreshed["draft_article_id"], article["id"])
        self.assertEqual(refreshed["status"], "draft_created")
        self.assertEqual(len(service.knowledge.get_drafts()), 1)

    def test_retry_recovers_draft_when_package_pointer_was_not_saved(self):
        service = CuratorResolutionService(self.root)
        package = service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        package["draft_article_id"] = None
        package["status"] = "prepared"
        service.packages.save(package)

        recovered = service.create_article_draft("GKT-TEST0", confirmed=True)

        self.assertEqual(recovered["id"], article["id"])
        self.assertEqual(service.get("GKT-TEST0")["draft_article_id"], article["id"])
        self.assertEqual(len(service.knowledge.get_drafts()), 1)

    def test_article_location_follows_draft_into_published_lifecycle(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        self.assertEqual(service.article_location("GKT-TEST0")[0], "draft")
        service.knowledge.publish_article(article["id"])

        state, located = service.article_location("GKT-TEST0")

        self.assertEqual(state, "published")
        self.assertEqual(located["id"], article["id"])

    def test_state_aware_open_route_reaches_draft_and_preserves_session_return(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        flask_app.config.update(TESTING=True)
        return_to = "/curator/fix/CFX-000000000001?category=editorial_opportunity"
        with patch("app.app.CuratorResolutionService", return_value=service), \
             patch("app.app.knowledge_repository", service.knowledge):
            with flask_app.test_client() as client:
                response = client.get(
                    f"/curator/tasks/GKT-TEST0/assisted-resolution/article"
                    f"?origin=maintenance&return_to={return_to}",
                    follow_redirects=True,
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn(article["title"].encode(), response.data)
        self.assertIn(b"Back to Fix Wizard", response.data)

    def test_saving_draft_keeps_relationship_proposal_only(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        before = json.loads((self.root / "app" / "workflow_drafts" / "test_workflow.json").read_text(encoding="utf-8"))
        saved = service.knowledge.get_draft(article["id"])
        saved["overview"] = "Reviewed but not published."
        service.knowledge.save_draft(saved, overwrite=True)
        after = json.loads((self.root / "app" / "workflow_drafts" / "test_workflow.json").read_text(encoding="utf-8"))

        self.assertEqual(before["nodes"]["step_one"].get("knowledge_article"), None)
        self.assertEqual(after["nodes"]["step_one"].get("knowledge_article"), None)

    def test_publication_finalizes_one_relationship_without_duplicate_linking(self):
        service = CuratorResolutionService(self.root)
        service.prepare("GKT-TEST0")
        article = service.create_article_draft("GKT-TEST0", confirmed=True)
        publisher = KnowledgePublicationService(
            service.knowledge,
            WorkflowDraftService(self.root / "app" / "workflow_drafts"),
        )

        publisher.publish(article["id"], reviewer="Reviewer")
        workflow = json.loads((self.root / "app" / "workflow_drafts" / "test_workflow.json").read_text(encoding="utf-8"))

        self.assertEqual(workflow["nodes"]["step_one"]["knowledge_article"], article["id"])
        self.assertEqual(service.article_location("GKT-TEST0")[0], "published")
        with self.assertRaises(ArticleNotFoundError):
            service.knowledge.get_draft(article["id"])

    def test_existing_article_is_recommended_for_link_not_duplicated(self):
        repository = KnowledgeRepository(self.root / "knowledge_base")
        article = {
            "id": "inspect-adapter", "title": "How to Inspect the Adapter", "category": "Networking",
            "difficulty": "Beginner", "estimated_time": "5 minutes", "overview": "Existing.",
            "checklist": [], "common_indicators": [], "commands": [], "related_topics": [], "quiz": [], "sources": [],
            "schema_version": "1.0", "generation": {"provider": None, "model": None, "generated_at": None},
            "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None, "notes": []},
        }
        repository.save_draft(article)
        package = CuratorResolutionService(self.root).prepare("GKT-TEST0")
        self.assertEqual(package["recommendation"], "LINK_EXISTING_ARTICLE")
        self.assertEqual(package["proposed_article_id"], "inspect-adapter")
        self.assertEqual(package["duplicate_confidence"], 100.0)
        self.assertEqual(package["canonical_recommendation"], "inspect-adapter")
        self.assertEqual(package["identity_resolution"]["status"], "matched")

    def test_published_canonical_is_reused_across_repeated_packages_without_numbered_ids(self):
        repository = KnowledgeRepository(self.root / "knowledge_base")
        article = {
            "id": "inspect-adapter", "canonical_id": "inspect-adapter",
            "title": "How to Inspect the Adapter", "category": "Networking",
            "difficulty": "Beginner", "estimated_time": "5 minutes", "overview": "Existing.",
            "checklist": [], "common_indicators": [], "commands": [], "related_topics": [],
            "quiz": [], "sources": [], "schema_version": "1.0",
            "generation": {"provider": None, "model": None, "generated_at": None},
            "review": {"status": "approved", "reviewed_by": "Reviewer", "reviewed_at": "2026-08-07T00:00:00+00:00", "notes": []},
        }
        repository.save_published(article)
        service = CuratorResolutionService(self.root)

        packages = [service.prepare(f"GKT-TEST{index}") for index in range(10)]

        self.assertTrue(all(item["recommendation"] == "LINK_EXISTING_ARTICLE" for item in packages))
        self.assertTrue(all(item["canonical_recommendation"] == "inspect-adapter" for item in packages))
        self.assertTrue(all(item["proposed_article_id"] == "inspect-adapter" for item in packages))
        self.assertTrue(all(item["proposed_relationship"]["action"] == "RELINK_EXISTING" for item in packages))
        self.assertFalse(any(item["proposed_article_id"].endswith(("-2", "-3", "-4")) for item in packages))

    def test_draft_creation_is_blocked_when_canonical_is_published_after_package_preparation(self):
        service = CuratorResolutionService(self.root)
        package = service.prepare("GKT-TEST0")
        self.assertEqual(package["recommendation"], "CREATE_NEW_ARTICLE")
        repository = KnowledgeRepository(self.root / "knowledge_base")
        article = {
            "id": package["proposed_article_id"], "title": package["proposed_article_title"],
            "category": "Networking", "overview": package["purpose"], "checklist": package["steps"],
        }
        repository.save_published(article)

        with self.assertRaisesRegex(ResolutionPackageError, "equivalent knowledge record already exists"):
            service.create_article_draft("GKT-TEST0", confirmed=True)

        self.assertEqual(repository.get_drafts(), [])

    def test_batch_selects_exactly_ten_and_rerun_refreshes(self):
        service = CuratorBatchService(self.root)
        first = service.prepare_first_batch()
        second = service.prepare_first_batch()
        self.assertEqual(first["selected"], 10)
        self.assertEqual(len(first["prepared"]), 10)
        self.assertEqual(len(second["prepared"]), 10)
        package = ResolutionPackageRepository(self.root / "curation_memory").get("GKT-TEST0")
        self.assertEqual(package["version"], 2)

    def test_batch_lock_prevents_duplicate_running_batch(self):
        service = CuratorBatchService(self.root)
        service.lock_path.write_text("running", encoding="utf-8")
        with self.assertRaises(ResolutionPackageError):
            service.prepare_first_batch()

    def test_validator_rejects_duplicate_id_and_missing_metadata(self):
        errors = AssistedResolutionValidator().validate({"proposed_article_id": "Bad ID"}, {"Bad ID"})
        self.assertTrue(any("Missing package field" in error for error in errors))
        self.assertTrue(any("lowercase" in error for error in errors))

    def test_validator_rejects_creation_without_failed_identity_resolution(self):
        service = CuratorResolutionService(self.root)
        package = service.prepare("GKT-TEST0")
        package["identity_resolution"] = {
            "status": "matched", "canonical_article_id": "inspect-adapter",
            "method": "normalized_title", "confidence": 100.0,
        }

        errors = AssistedResolutionValidator().validate(package, set())

        self.assertIn("A new article may be proposed only after identity resolution returns no match.", errors)

    def test_validator_rejects_numbered_variant_when_canonical_id_exists(self):
        service = CuratorResolutionService(self.root)
        package = service.prepare("GKT-TEST0")
        canonical_id = package["proposed_article_id"]
        package["proposed_article_id"] = f"{canonical_id}-2"
        package["proposed_relationship"]["target_article_id"] = f"{canonical_id}-2"

        errors = AssistedResolutionValidator().validate(package, {canonical_id})

        self.assertIn(
            f"Numbered duplicate IDs are not allowed while canonical article '{canonical_id}' exists.",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
