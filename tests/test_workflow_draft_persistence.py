import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from app.services.workflow_draft_persistence import (
    DRAFT_PERSISTENCE_FAILURES,
    WorkflowDraftPersistence,
    WorkflowDraftPersistenceError,
)
from app.services.workflow_draft_service import WorkflowDraftService


def _hold_draft_lock(drafts_path, filename, ready, release):
    persistence = WorkflowDraftPersistence(Path(drafts_path))
    with persistence.locked(filename, timeout=2):
        ready.set()
        release.wait(5)


class WorkflowDraftPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.persistence = WorkflowDraftPersistence(self.drafts)
        self.workflow = {
            "workflow_id": "sample", "name": "Sample", "estimated_steps": 1,
            "start_node": "done", "nodes": {
                "done": {"type": "resolution", "title": "Done", "message": "Complete."}
            },
        }
        self.snapshot = self.persistence.save("sample.json", self.workflow)

    def tearDown(self):
        self.temporary.cleanup()

    def _process_lock(self, filename="sample.json"):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_hold_draft_lock,
            args=(str(self.drafts), filename, ready, release),
        )
        process.start()
        self.assertTrue(ready.wait(5), "child process did not acquire the draft lock")
        return process, release

    def test_failure_taxonomy_is_bounded(self):
        self.assertEqual(DRAFT_PERSISTENCE_FAILURES, {
            "draft_not_found", "invalid_draft_path", "lock_unavailable", "stale_workflow",
            "persistence_failed", "verification_failed", "restore_failed",
        })

    def test_same_workflow_lock_conflicts_across_processes_and_times_out(self):
        process, release = self._process_lock()
        try:
            with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                with self.persistence.locked("sample.json", timeout=0.1):
                    self.fail("conflicting lock unexpectedly succeeded")
            self.assertEqual(caught.exception.code, "lock_unavailable")
        finally:
            release.set(); process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_different_workflows_lock_independently(self):
        process, release = self._process_lock()
        try:
            with self.persistence.locked("other.json", timeout=0.1):
                pass
        finally:
            release.set(); process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_lock_releases_after_normal_exit_and_exception(self):
        with self.persistence.locked("sample.json", timeout=0.1):
            pass
        with self.persistence.locked("sample.json", timeout=0.1):
            pass
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with self.persistence.locked("sample.json", timeout=0.1):
                raise RuntimeError("stop")
        with self.persistence.locked("sample.json", timeout=0.1):
            pass

    def test_lock_identity_is_stable_and_outside_draft_enumeration(self):
        expected = self.drafts.parent / ".workflow_draft_locks" / "sample.json.lock"
        with self.persistence.locked("sample.json"):
            self.assertTrue(expected.is_file())
        self.assertEqual(list(self.drafts.glob("*.lock")), [])
        for invalid in ("../sample.json", "C:\\sample.json", "sample.txt", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                    with self.persistence.locked(invalid):
                        pass
                self.assertEqual(caught.exception.code, "invalid_draft_path")

    def test_matching_raw_fingerprint_replaces_and_returns_verified_snapshots(self):
        updated = {**self.workflow, "name": "Updated"}
        result = self.persistence.compare_and_swap(
            "sample.json", self.snapshot.raw_sha256, updated,
        )
        self.assertEqual(result.before.content, self.snapshot.content)
        self.assertNotEqual(result.before.raw_sha256, result.after.raw_sha256)
        self.assertEqual(result.after.workflow["name"], "Updated")
        self.assertEqual(result.after.content, (self.drafts / "sample.json").read_bytes())

    def test_stale_and_formatting_only_changes_fail_without_writing(self):
        path = self.drafts / "sample.json"
        formatting_edit = json.dumps(self.workflow, separators=(",", ":")).encode()
        path.write_bytes(formatting_edit)
        before = path.read_bytes()
        self.assertEqual(
            StructuralRepairFingerprint.semantic_workflow(json.loads(before)),
            self.snapshot.semantic_sha256,
        )
        with self.assertRaises(WorkflowDraftPersistenceError) as caught:
            self.persistence.compare_and_swap("sample.json", self.snapshot.raw_sha256,
                                              {**self.workflow, "name": "Lost update"})
        self.assertEqual(caught.exception.code, "stale_workflow")
        self.assertEqual(path.read_bytes(), before)

    def test_missing_invalid_and_non_draft_targets_fail_closed(self):
        with self.assertRaises(WorkflowDraftPersistenceError) as missing:
            self.persistence.compare_and_swap("missing.json", "a" * 64, self.workflow)
        self.assertEqual(missing.exception.code, "draft_not_found")
        for value in ("../decision_trees/sample.json", "../workflow_publications/sample.json"):
            with self.assertRaises(WorkflowDraftPersistenceError) as invalid:
                self.persistence.compare_and_swap(value, self.snapshot.raw_sha256, self.workflow)
            self.assertEqual(invalid.exception.code, "invalid_draft_path")

    def test_failure_before_replace_preserves_original_active_file(self):
        path = self.drafts / "sample.json"
        before = path.read_bytes()
        with patch.object(self.persistence, "_atomic_replace", side_effect=OSError("disk")):
            with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                self.persistence.compare_and_swap("sample.json", self.snapshot.raw_sha256,
                                                  {**self.workflow, "name": "Not written"})
        self.assertEqual(caught.exception.code, "persistence_failed")
        self.assertEqual(path.read_bytes(), before)

    def test_post_write_reread_detects_unexpected_content(self):
        original = self.snapshot.content
        unexpected = json.dumps({**self.workflow, "name": "Unexpected"}).encode()
        with patch.object(self.persistence, "_read_bytes", side_effect=[original, unexpected]):
            with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                self.persistence.compare_and_swap("sample.json", self.snapshot.raw_sha256,
                                                  {**self.workflow, "name": "Requested"})
        self.assertEqual(caught.exception.code, "verification_failed")

    def test_exact_backup_restore_and_third_state_protection(self):
        original = self.snapshot.content
        changed = self.persistence.compare_and_swap(
            "sample.json", self.snapshot.raw_sha256, {**self.workflow, "name": "Changed"}
        )
        with self.persistence.locked("sample.json") as draft:
            restored = draft.restore(changed.after.raw_sha256, original)
        self.assertEqual(restored.after.content, original)

        third = self.persistence.compare_and_swap(
            "sample.json", restored.after.raw_sha256, {**self.workflow, "name": "Third"}
        )
        with self.persistence.locked("sample.json") as draft:
            with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                draft.restore(changed.after.raw_sha256, original)
        self.assertEqual(caught.exception.code, "stale_workflow")
        self.assertEqual((self.drafts / "sample.json").read_bytes(), third.after.content)

    def test_restore_failure_is_explicit(self):
        changed = self.persistence.compare_and_swap(
            "sample.json", self.snapshot.raw_sha256, {**self.workflow, "name": "Changed"}
        )
        with self.persistence.locked("sample.json") as draft:
            with patch.object(self.persistence, "_atomic_replace", side_effect=OSError("disk")):
                with self.assertRaises(WorkflowDraftPersistenceError) as caught:
                    draft.restore(changed.after.raw_sha256, self.snapshot.content)
        self.assertEqual(caught.exception.code, "restore_failed")

    def test_existing_workflow_draft_writer_create_and_update_are_compatible(self):
        service = WorkflowDraftService(self.drafts)
        workflow = {**self.workflow, "workflow_id": "editor", "name": "Editor"}
        filename = service.save_draft(workflow)
        updated = service.update_node(filename, "done", {"message": "Updated safely."})
        settings = service.update_settings(filename, {
            "name": "Editor Updated", "description": "Description", "estimated_steps": 1,
            "start_node": "done", "category": "Other", "platform": "Cross-platform",
        })
        self.assertEqual(updated["nodes"]["done"]["message"], "Updated safely.")
        self.assertEqual(settings["name"], "Editor Updated")
        self.assertEqual(service.get_draft(filename), settings)


if __name__ == "__main__":
    unittest.main()
