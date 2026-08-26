import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
)
from app.services.curator_structural_repair_governance import (
    StructuralRepairApplicationRecord,
    StructuralRepairFingerprint,
)
from app.services.workflow_lifecycle_projection_service import (
    AMBIGUOUS_STATE,
    AUTHORED_OR_UNATTRIBUTED_CHANGES,
    GOVERNED_CHANGES,
    MATCHES_PUBLISHED,
    MIXED_CHANGES,
    NO_ACTIVE_PUBLICATION,
    NO_UNPUBLISHED_CHANGES,
    NOT_READY,
    READY_FOR_PUBLICATION_REVIEW,
    WorkflowLifecycleProjectionService,
    WorkflowRuntimeProjection,
)


def workflow(workflow_id="demo"):
    return {
        "workflow_id": workflow_id,
        "name": "Lifecycle Demo",
        "description": "A bounded lifecycle fixture.",
        "start_node": "inspect",
        "estimated_steps": 2,
        "progress_mode": "branch_aware",
        "nodes": {
            "inspect": {
                "type": "instruction",
                "title": "Inspect status",
                "instruction": "Inspect the current status without changing it.",
                "next": "done",
            },
            "done": {
                "type": "resolution",
                "title": "Inspection complete",
                "message": "The inspection is complete.",
            },
        },
    }


class FakeApplications:
    def __init__(self, records=()):
        self.records = {item.application_id: (item,) for item in records}

    def list_application_ids(self):
        return tuple(sorted(self.records))

    def get(self, application_id):
        return self.records.get(application_id, ())


class FakeRecoveries:
    def __init__(self, materials=None, events=None):
        self.materials = materials or {}
        self.event_values = events or {}

    def get(self, application_id):
        return self.materials[application_id]

    def events(self, application_id):
        return tuple(self.event_values.get(application_id, ()))


def record(application_id, before, after, *, metadata=(), nodes=(), edges=(),
           outcome="applied", finalized=True):
    before_bytes = json.dumps(before).encode("utf-8")
    after_bytes = json.dumps(after).encode("utf-8")
    return SimpleNamespace(
        application_id=application_id,
        workflow_id=str(before.get("workflow_id") or after.get("workflow_id")),
        workflow_semantic_sha256_before=fingerprint(before),
        expected_workflow_semantic_sha256_after=fingerprint(after),
        workflow_raw_sha256_before=hashlib.sha256(before_bytes).hexdigest(),
        expected_workflow_raw_sha256_after=hashlib.sha256(after_bytes).hexdigest(),
        workflow_path="app/workflow_drafts/demo.json",
        metadata_changes=tuple(metadata),
        proposed_node_ids=tuple(nodes),
        changed_edges=tuple(edges),
        outcome=outcome,
        finalized_at="2026-08-25T12:00:00+00:00" if finalized else "",
    )


def material(item, before):
    original = json.dumps(before).encode("utf-8")
    return {
        "application_id": item.application_id,
        "workflow_id": item.workflow_id,
        "workflow_path": item.workflow_path,
        "original_bytes": original,
        "workflow_raw_sha256_before": hashlib.sha256(original).hexdigest(),
        "expected_workflow_raw_sha256_after": item.expected_workflow_raw_sha256_after,
        "expected_workflow_semantic_sha256_after": item.expected_workflow_semantic_sha256_after,
    }


def fingerprint(value):
    return WorkflowLifecycleProjectionService._fingerprint(value)


class WorkflowLifecycleProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "app" / "workflow_drafts").mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def write_draft(self, value, filename="demo.json"):
        path = self.root / "app" / "workflow_drafts" / filename
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def publish(self, value, version=1):
        directory = self.root / "app" / "workflow_publications" / value["workflow_id"]
        directory.mkdir(parents=True)
        content_hash = hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        snapshot = {
            "publication": {
                "version": version, "source_filename": f"{value['workflow_id']}.json",
                "content_hash": content_hash,
            },
            "workflow": value,
        }
        (directory / f"v{version:04d}.json").write_text(
            json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "current.json").write_text(json.dumps({
            "workflow_id": value["workflow_id"], "current_version": version,
            "content_hash": content_hash,
        }, indent=2) + "\n", encoding="utf-8")

    def service(self, records=(), materials=None, events=None, runtime_selector=None):
        return WorkflowLifecycleProjectionService(
            self.root,
            application_repository=FakeApplications(records),
            recovery_repository=FakeRecoveries(materials, events),
            runtime_selector=runtime_selector,
        )

    def test_matching_draft_has_no_unpublished_changes(self):
        value = workflow()
        self.write_draft(value)
        self.publish(value)

        result = self.service().project("demo")

        self.assertEqual(result.lifecycle_state, MATCHES_PUBLISHED)
        self.assertEqual(result.publication_review_state, NO_UNPUBLISHED_CHANGES)
        self.assertEqual(result.semantic_delta, ())
        self.assertEqual(result.active_published_version, 1)
        self.assertTrue(result.runtime.matches_active_publication)

    def test_governed_metadata_delta_uses_continuous_fingerprint_chain(self):
        before = workflow()
        before.pop("progress_mode")
        after = deepcopy(before)
        after["progress_mode"] = "branch_aware"
        self.publish(before)
        self.write_draft(after)
        item = record("SRX-1111111111111111", before, after, metadata=({
            "path": "/progress_mode", "before_present": False,
            "before_value": None, "after_value": "branch_aware",
        },))

        result = self.service(
            (item,), {item.application_id: material(item, before)}
        ).project("demo")

        self.assertEqual(result.lifecycle_state, GOVERNED_CHANGES)
        self.assertEqual(result.publication_review_state, READY_FOR_PUBLICATION_REVIEW)
        self.assertEqual({entry.path for entry in result.semantic_delta}, {"/progress_mode"})
        self.assertTrue(all(entry.provenance == "governed" for entry in result.semantic_delta))

    def test_real_application_and_recovery_repositories_are_correlated_read_only(self):
        before = workflow()
        before.pop("progress_mode")
        after = deepcopy(before)
        after["progress_mode"] = "branch_aware"
        self.publish(before)
        self.write_draft(after)
        curator_root = self.root / "curation_memory"
        applications = StructuralRepairApplicationRepository(curator_root)
        recoveries = StructuralRepairRecoveryRepository(curator_root)
        before_bytes = (json.dumps(before, indent=2) + "\n").encode("utf-8")
        after_bytes = (json.dumps(after, indent=2) + "\n").encode("utf-8")
        base = {
            "schema_version": "3.0",
            "application_id": "SRX-8888888888888888",
            "approval_id": "SRA-8888888888888888",
            "event_id": "SRE-8888888888888888",
            "revision": 1,
            "previous_event_digest": "",
            "task_id": "GKT-LIFECYCLE",
            "finding_id": "CUR-LIFECYCLE",
            "fix_session_id": "CFX-LIFECYCLE",
            "reviewer_identity": "Lifecycle Reviewer",
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": "demo",
            "workflow_path": "app/workflow_drafts/demo.json",
            "workflow_raw_sha256_before": StructuralRepairFingerprint.raw_workflow(before_bytes),
            "workflow_semantic_sha256_before": fingerprint(before),
            "expected_workflow_raw_sha256_after": StructuralRepairFingerprint.raw_workflow(after_bytes),
            "expected_workflow_semantic_sha256_after": fingerprint(after),
            "preview_digest": "a" * 64,
            "plan_digest": "b" * 64,
            "adapter_id": "progress_metadata_branch_aware",
            "specification_id": "branch-aware-progress-metadata-v1",
            "specification_version": 1,
            "specification_digest": "c" * 64,
            "proposed_node_ids": [],
            "changed_edges": [],
            "new_edges": [],
            "metadata_changes": [{
                "path": "/progress_mode", "before_present": False,
                "before_value": None, "after_value": "branch_aware",
            }],
            "created_at": "2026-08-25T12:00:00+00:00",
            "applied_at": "",
            "finalized_at": "",
            "validation_summaries": {"passed": True},
            "outcome": "pending",
            "failure_category": "",
            "failure_reason": "",
            "rollback_status": "not_required",
            "rollback_raw_sha256": "",
            "rollback_semantic_sha256": "",
        }
        pending = StructuralRepairApplicationRecord.from_dict(base)
        applications.append(pending)
        recoveries.capture(
            application_id=base["application_id"], approval_id=base["approval_id"],
            task_id=base["task_id"], finding_id=base["finding_id"],
            fix_session_id=base["fix_session_id"], reviewer_identity=base["reviewer_identity"],
            workflow_id="demo", workflow_path=base["workflow_path"],
            original_bytes=before_bytes, raw_before=base["workflow_raw_sha256_before"],
            semantic_before=base["workflow_semantic_sha256_before"],
            expected_raw_after=base["expected_workflow_raw_sha256_after"],
            expected_semantic_after=base["expected_workflow_semantic_sha256_after"],
            captured_at=base["created_at"],
        )
        applied_data = dict(base)
        applied_data.update({
            "event_id": "SRE-9999999999999999", "revision": 2,
            "previous_event_digest": pending.event_digest,
            "applied_at": "2026-08-25T12:01:00+00:00",
            "finalized_at": "2026-08-25T12:01:01+00:00", "outcome": "applied",
        })
        applications.append(applied_data)
        before_files = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

        result = WorkflowLifecycleProjectionService(self.root).project("demo")

        after_files = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(result.lifecycle_state, GOVERNED_CHANGES)
        self.assertEqual(after_files, before_files)

    def test_governed_graph_delta_is_supported(self):
        before = workflow()
        after = deepcopy(before)
        after["nodes"]["inspect"]["next"] = "verify"
        after["nodes"]["verify"] = {
            "type": "question", "question": "Is the inspection complete?",
            "answers": {
                "yes": {"label": "Yes", "next": "done"},
                "no": {"label": "No", "next": "done"},
            },
        }
        self.publish(before)
        self.write_draft(after)
        edge = SimpleNamespace(source="inspect", route="next", destination="done")
        item = record("SRX-2222222222222222", before, after, nodes=("verify",), edges=(edge,))

        result = self.service(
            (item,), {item.application_id: material(item, before)}
        ).project("demo")

        self.assertEqual(result.lifecycle_state, GOVERNED_CHANGES)
        self.assertTrue(result.governed_delta_summary)

    def test_human_authored_delta_is_distinguished_and_can_be_ready(self):
        before = workflow()
        after = deepcopy(before)
        after["description"] = "A reviewed human-authored description."
        self.publish(before)
        self.write_draft(after)

        result = self.service().project("demo")

        self.assertEqual(result.lifecycle_state, AUTHORED_OR_UNATTRIBUTED_CHANGES)
        self.assertEqual(result.publication_review_state, READY_FOR_PUBLICATION_REVIEW)
        self.assertEqual(result.semantic_delta[0].provenance, "authored_or_unattributed")
        self.assertTrue(result.authored_or_unattributed_delta_summary)
        with self.assertRaises(FrozenInstanceError):
            result.lifecycle_state = "NOT_IMMUTABLE"

    def test_semantic_delta_is_deterministic_and_covers_add_remove_replace(self):
        before = {
            "removed": True,
            "replaced": "before",
            "nested/key": {"ordered": [1, 2]},
        }
        after = {
            "added": True,
            "replaced": "after",
            "nested/key": {"ordered": [1, 3]},
        }

        first = WorkflowLifecycleProjectionService._semantic_delta(before, after)
        second = WorkflowLifecycleProjectionService._semantic_delta(before, after)

        self.assertEqual(first, second)
        self.assertEqual(
            [(item.operation, item.path) for item in first],
            [
                ("add", "/added"),
                ("replace", "/nested~1key/ordered/1"),
                ("remove", "/removed"),
                ("replace", "/replaced"),
            ],
        )
        self.assertTrue(all(len(item.before_fingerprint) == 64 for item in first))
        self.assertTrue(all(len(item.after_fingerprint) == 64 for item in first))

    def test_mixed_delta_marks_only_declared_repair_path_governed(self):
        published = workflow()
        published.pop("progress_mode")
        authored = deepcopy(published)
        authored["description"] = "Human-authored pending text."
        draft = deepcopy(authored)
        draft["progress_mode"] = "branch_aware"
        self.publish(published)
        self.write_draft(draft)
        item = record("SRX-3333333333333333", authored, draft, metadata=({
            "path": "/progress_mode", "before_present": False,
            "before_value": None, "after_value": "branch_aware",
        },))

        result = self.service(
            (item,), {item.application_id: material(item, authored)}
        ).project("demo")

        self.assertEqual(result.lifecycle_state, MIXED_CHANGES)
        by_path = {entry.path: entry.provenance for entry in result.semantic_delta}
        self.assertEqual(by_path["/progress_mode"], "governed")
        self.assertEqual(by_path["/description"], "authored_or_unattributed")

    def test_branching_governed_history_fails_closed(self):
        before = workflow()
        draft = deepcopy(before)
        draft["description"] = "First governed candidate."
        alternate = deepcopy(before)
        alternate["description"] = "Second governed candidate."
        self.publish(before)
        self.write_draft(draft)
        first = record("SRX-4444444444444444", before, draft, nodes=("done",))
        second = record("SRX-5555555555555555", before, alternate, nodes=("done",))

        result = self.service(
            (first, second), {
                first.application_id: material(first, before),
                second.application_id: material(second, before),
            },
        ).project("demo")

        self.assertEqual(result.lifecycle_state, AMBIGUOUS_STATE)
        self.assertEqual(result.publication_review_state, NOT_READY)
        self.assertTrue(any("branches" in reason for reason in result.ambiguity_reasons))

    def test_pending_and_recovered_provenance_fail_closed(self):
        before = workflow()
        after = deepcopy(before)
        after["description"] = "Pending change."
        self.publish(before)
        self.write_draft(after)
        pending = record(
            "SRX-6666666666666666", before, after,
            outcome="pending", finalized=False,
        )
        pending_result = self.service((pending,)).project("demo")
        self.assertEqual(pending_result.lifecycle_state, AMBIGUOUS_STATE)

        applied = record("SRX-7777777777777777", before, after, nodes=("done",))
        recovered_result = self.service(
            (applied,), {applied.application_id: material(applied, before)},
            {applied.application_id: ({"outcome": "recovered"},)},
        ).project("demo")
        self.assertEqual(recovered_result.lifecycle_state, AMBIGUOUS_STATE)

    def test_no_active_publication_is_not_ready(self):
        self.write_draft(workflow())
        result = self.service().project("demo")
        self.assertEqual(result.lifecycle_state, NO_ACTIVE_PUBLICATION)
        self.assertEqual(result.publication_review_state, NOT_READY)

    def test_schema_graph_progress_and_reasoning_defects_block_readiness(self):
        cases = []
        invalid_schema = workflow()
        invalid_schema["nodes"]["inspect"]["next"] = "missing"
        cases.append(invalid_schema)
        progress = workflow()
        progress.pop("progress_mode")
        progress["estimated_steps"] = 1
        cases.append(progress)
        reasoning = workflow()
        reasoning["nodes"]["inspect"].update({
            "title": "Restart application",
            "instruction": "Restart the application.",
        })
        cases.append(reasoning)

        for index, draft in enumerate(cases):
            with self.subTest(index=index):
                root = self.root / str(index)
                (root / "app" / "workflow_drafts").mkdir(parents=True)
                service = WorkflowLifecycleProjectionService(
                    root, application_repository=FakeApplications(),
                    recovery_repository=FakeRecoveries(),
                )
                published = workflow()
                self._write_to_root(root, draft, published)
                result = service.project("demo")
                self.assertEqual(result.publication_review_state, NOT_READY)

    def test_runtime_mismatch_blocks_and_overlay_is_disclosed(self):
        value = workflow()
        changed = deepcopy(value)
        changed["description"] = "Unpublished description."
        self.publish(value)
        self.write_draft(changed)
        selector = lambda *_args: WorkflowRuntimeProjection(99, False, False)
        mismatch = self.service(runtime_selector=selector).project("demo")
        self.assertEqual(mismatch.lifecycle_state, AMBIGUOUS_STATE)
        self.assertEqual(mismatch.publication_review_state, NOT_READY)

        overlay = workflow("network_diagnostics")
        overlay["nodes"] = {
            "advanced_complete": {
                "type": "resolution", "title": "Complete", "message": "Continue diagnostics."
            }
        }
        overlay["start_node"] = "advanced_complete"
        overlay["estimated_steps"] = 1
        self.write_draft(overlay, "network_diagnostics.json")
        self.publish(overlay)
        projected = self.service().project("network_diagnostics")
        self.assertTrue(projected.runtime.runtime_overlay_present)
        self.assertEqual(projected.lifecycle_state, MATCHES_PUBLISHED)

    def test_concurrent_change_and_multiple_drafts_fail_closed(self):
        value = workflow()
        self.publish(value)
        self.write_draft(value)
        service = self.service()
        with patch.object(service, "_draft_unchanged", return_value=False):
            result = service.project("demo")
        self.assertEqual(result.lifecycle_state, AMBIGUOUS_STATE)

        self.write_draft(value, "duplicate.json")
        result = self.service().project("demo")
        self.assertEqual(result.lifecycle_state, AMBIGUOUS_STATE)
        self.assertTrue(any("Multiple" in reason for reason in result.ambiguity_reasons))

    def test_projection_creates_no_files_or_state(self):
        value = workflow()
        self.write_draft(value)
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.service().project("demo")
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse((self.root / "curation_memory").exists())
        self.assertFalse((self.root / "app" / ".workflow_draft_locks").exists())

    @staticmethod
    def _write_to_root(root, draft, published):
        (root / "app" / "workflow_drafts" / "demo.json").write_text(
            json.dumps(draft, indent=2) + "\n", encoding="utf-8"
        )
        directory = root / "app" / "workflow_publications" / "demo"
        directory.mkdir(parents=True)
        content_hash = hashlib.sha256(json.dumps(
            published, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        (directory / "v0001.json").write_text(json.dumps({
            "publication": {"version": 1, "content_hash": content_hash},
            "workflow": published,
        }), encoding="utf-8")
        (directory / "current.json").write_text(json.dumps({
            "current_version": 1, "content_hash": content_hash,
        }), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
