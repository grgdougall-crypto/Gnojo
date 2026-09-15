import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.repositories.knowledge_repository import KnowledgeRepositoryError
from app.services.published_article_promotion_service import (
    PublishedArticlePromotionService,
)
from curator.__main__ import main


class PublishedArticlePromotionTests(unittest.TestCase):
    ARTICLE_ID = "reviewed-article"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "data"
        (self.source / "knowledge_base/published").mkdir(parents=True)
        for relative in (
            "knowledge_base/published", "knowledge_base/drafts",
            "knowledge_base/archive", "knowledge_base/deleted",
            "knowledge_base/commands", "knowledge_base/scripts",
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
        ):
            (self.target / relative).mkdir(parents=True)
        (self.target / "knowledge_base/aliases.json").write_text(
            json.dumps({"schema_version": "1.0", "aliases": {}}), encoding="utf-8"
        )
        (self.target / "knowledge_base/inventory.json").write_text(
            json.dumps({"schema_version": "1.0", "articles": []}), encoding="utf-8"
        )
        self.article = self._article(self.ARTICLE_ID)
        self.source_path = self.source / "knowledge_base/published" / f"{self.ARTICLE_ID}.json"
        self._write_source(self.article)
        self.service = PublishedArticlePromotionService(
            source_root=self.source, data_root=self.target
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _article(article_id, *, title="Reviewed Article"):
        timestamp = "2026-09-15T00:00:00+00:00"
        return {
            "schema_version": "1.0",
            "id": article_id,
            "canonical_id": article_id,
            "title": title,
            "category": "Desktop Support",
            "difficulty": "Beginner",
            "estimated_time": "5 minutes",
            "overview": "A reviewed operational article.",
            "tags": ["desktop", "support", "reviewed"],
            "checklist": ["Inspect the current state."],
            "common_indicators": ["The condition is visible."],
            "commands": [],
            "related_topics": ["Support"],
            "quiz": [{
                "question": "What should you inspect?",
                "answers": ["Current state", "Nothing"],
                "correct_answer": "Current state",
            }],
            "sources": [{"title": "Official source", "url": "https://example.test/source"}],
            "generation": {"provider": "Test", "model": "deterministic", "generated_at": timestamp},
            "review": {
                "status": "approved", "reviewed_by": "Reviewer", "reviewed_at": timestamp,
                "notes": ["Reviewed."],
                "checks": {
                    "technical_accuracy": True, "user_safety": True,
                    "sources_verified": True, "commands_reviewed": True,
                },
            },
            "version": 1,
            "published_at": timestamp,
            "version_history": [{"version": 1, "published_at": timestamp, "reviewed_by": "Reviewer"}],
        }

    def _write_source(self, article):
        self.source_path.write_text(json.dumps(article, indent=2), encoding="utf-8")

    @staticmethod
    def _fingerprint(root):
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
        }

    def test_dry_run_is_write_free_and_reports_safe_details(self):
        before_source = self._fingerprint(self.source)
        before_target = self._fingerprint(self.target)

        result = self.service.preview(self.ARTICLE_ID)

        self.assertEqual(result["result"], "SAFE_TO_PROMOTE")
        self.assertEqual(result["current_published_article_count"], 0)
        self.assertEqual(result["projected_published_article_count"], 1)
        self.assertTrue(result["inventory_rebuild_required"])
        self.assertEqual(result["reviewer"], "Reviewer")
        self.assertEqual(self._fingerprint(self.source), before_source)
        self.assertEqual(self._fingerprint(self.target), before_target)

    def test_invalid_approval_metadata_blocks(self):
        mutations = (
            ("approval", lambda item: item["review"].update(status="pending_review")),
            ("reviewer", lambda item: item["review"].update(reviewed_by="")),
            ("timestamp", lambda item: item["review"].update(reviewed_at="")),
            ("checks", lambda item: item["review"]["checks"].update(user_safety=False)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                article = deepcopy(self.article)
                mutate(article)
                self._write_source(article)
                self.assertEqual(self.service.preview(self.ARTICLE_ID)["result"], "BLOCKED")

    def test_invalid_schema_version_or_history_blocks(self):
        mutations = (
            ("schema", lambda item: item.update(schema_version="0.1")),
            ("version", lambda item: item.update(version=0)),
            ("history", lambda item: item.update(version_history=[])),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                article = deepcopy(self.article)
                mutate(article)
                self._write_source(article)
                self.assertEqual(self.service.preview(self.ARTICLE_ID)["result"], "BLOCKED")

    def test_filename_id_and_canonical_mismatch_block(self):
        for field in ("id", "canonical_id"):
            with self.subTest(field=field):
                article = deepcopy(self.article)
                article[field] = "different-article"
                self._write_source(article)
                result = self.service.preview(self.ARTICLE_ID)
                self.assertEqual(result["result"], "BLOCKED")
                self.assertTrue(any("match" in reason for reason in result["blocking_reasons"]))

    def test_exact_target_collision_blocks_without_overwrite(self):
        target = self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json"
        target.write_text(json.dumps(self.article), encoding="utf-8")
        before = target.read_bytes()

        result = self.service.apply(self.ARTICLE_ID)

        self.assertEqual(result["result"], "BLOCKED")
        self.assertTrue(result["exact_target_exists"])
        self.assertEqual(target.read_bytes(), before)

    def test_canonical_equivalent_collision_blocks(self):
        existing = self._article("existing-article", title=self.article["title"])
        path = self.target / "knowledge_base/published/existing-article.json"
        path.write_text(json.dumps(existing), encoding="utf-8")

        result = self.service.preview(self.ARTICLE_ID)

        self.assertEqual(result["result"], "BLOCKED")
        self.assertTrue(result["canonical_collision_exists"])
        self.assertEqual(result["canonical_collision_identity"], "existing-article")

    def test_alias_collision_blocks(self):
        (self.target / "knowledge_base/aliases.json").write_text(json.dumps({
            "schema_version": "1.0",
            "aliases": {self.ARTICLE_ID: "existing-article"},
        }), encoding="utf-8")

        result = self.service.preview(self.ARTICLE_ID)

        self.assertEqual(result["result"], "BLOCKED")
        self.assertTrue(result["canonical_collision_exists"])
        self.assertEqual(result["canonical_collision_identity"], "existing-article")

    def test_uninitialized_target_blocks_without_creating_directories(self):
        target = self.root / "uninitialized"
        service = PublishedArticlePromotionService(
            source_root=self.source, data_root=target
        )

        result = service.preview(self.ARTICLE_ID)

        self.assertEqual(result["result"], "BLOCKED")
        self.assertFalse(target.exists())

    def test_apply_writes_one_article_rebuilds_inventory_and_replay_blocks(self):
        unrelated = self.target / "keep.txt"
        unrelated.write_text("unchanged", encoding="utf-8")
        source_before = self.source_path.read_bytes()

        result = self.service.apply(self.ARTICLE_ID)

        self.assertEqual(result["result"], "PROMOTED")
        self.assertEqual(result["after_published_article_count"], 1)
        self.assertEqual(result["inventory_result"], "REBUILT_AND_VERIFIED")
        inventory = json.loads((self.target / "knowledge_base/inventory.json").read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in inventory["articles"]], [self.ARTICLE_ID])
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(self.source_path.read_bytes(), source_before)

        target = self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json"
        target_before = target.read_bytes()
        replay = self.service.apply(self.ARTICLE_ID)
        self.assertEqual(replay["result"], "BLOCKED")
        self.assertEqual(target.read_bytes(), target_before)

    def test_inventory_failure_rolls_back_article_and_inventory(self):
        inventory = self.target / "knowledge_base/inventory.json"
        inventory_before = inventory.read_bytes()
        with patch(
            "app.services.published_article_promotion_service.KnowledgeIntegrityService.rebuild_index",
            side_effect=OSError("inventory failure"),
        ):
            result = self.service.apply(self.ARTICLE_ID)

        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(result["rollback_result"], "VERIFIED")
        self.assertFalse((self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json").exists())
        self.assertEqual(inventory.read_bytes(), inventory_before)

    def test_repository_failure_leaves_production_state_unchanged(self):
        before = self._fingerprint(self.target)
        with patch(
            "app.services.published_article_promotion_service.KnowledgeRepository.save_published",
            side_effect=KnowledgeRepositoryError("write failure"),
        ):
            result = self.service.apply(self.ARTICLE_ID)
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(self._fingerprint(self.target), before)

    def test_source_hash_change_before_write_fails_closed(self):
        original = self.source_path.read_bytes()
        changed = original + b" "
        with patch.object(self.service, "_read_source_bytes", side_effect=[original, changed]):
            result = self.service.apply(self.ARTICLE_ID)
        self.assertEqual(result["result"], "BLOCKED")
        self.assertIn("changed after validation", result["blocking_reasons"][0])
        self.assertFalse((self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json").exists())

    def test_source_hash_change_immediately_before_mutation_fails_closed(self):
        original = self.source_path.read_bytes()
        changed = original + b" "
        with patch.object(
            self.service, "_read_source_bytes",
            side_effect=[original, original, changed],
        ):
            result = self.service.apply(self.ARTICLE_ID)
        self.assertEqual(result["result"], "BLOCKED")
        self.assertIn("immediately before mutation", result["blocking_reasons"][0])
        self.assertFalse((self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json").exists())

    def test_cli_defaults_to_dry_run_and_apply_is_explicit(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(self.target)}), patch(
            "app.services.published_article_promotion_service.APPLICATION_ROOT", self.source
        ), redirect_stdout(output), redirect_stderr(errors):
            status = main(["promote-published-article", "--article", self.ARTICLE_ID])
        self.assertEqual(status, 0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry_run")
        self.assertFalse((self.target / "knowledge_base/published" / f"{self.ARTICLE_ID}.json").exists())


if __name__ == "__main__":
    unittest.main()
