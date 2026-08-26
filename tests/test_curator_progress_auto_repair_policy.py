import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.app import app as flask_app
from app.services.curator_progress_auto_repair_policy_service import (
    CuratorProgressAutoRepairPolicyService,
)
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from app.services.workflow_lifecycle_projection_service import (
    AMBIGUOUS_STATE,
    AUTHORED_OR_UNATTRIBUTED_CHANGES,
    GOVERNED_CHANGES,
    MIXED_CHANGES,
    NO_ACTIVE_PUBLICATION,
    NOT_READY,
    SemanticDeltaOperation,
    WorkflowRuntimeProjection,
)
from curator.memory import CuratorMemoryStore


class FakeApplicationRepository:
    def __init__(self, records=()):
        self.records = tuple(records)

    def list_application_ids(self):
        return tuple(f"SRX-FAKE{index:012d}" for index, _ in enumerate(self.records, 1))

    def get(self, application_id):
        index = int(application_id.removeprefix("SRX-FAKE")) - 1
        return (self.records[index],)

    def append(self, record):  # pragma: no cover - capability marker; evaluator never calls it
        raise AssertionError("Read-only evaluator must not append application state")


class CuratorProgressAutoRepairPolicyTests(unittest.TestCase):
    task_id = "GKT-POLICY-PROGRESS"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous_repository_root = flask_app.config.get(
            "STRUCTURAL_REPAIR_REPOSITORY_ROOT"
        )
        flask_app.config.update(
            TESTING=True,
            STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root),
        )
        self.client = flask_app.test_client()
        self.drafts = self.root / "app" / "workflow_drafts"
        self.publications = self.root / "app" / "workflow_publications" / "policy_progress"
        self.drafts.mkdir(parents=True)
        self.publications.mkdir(parents=True)
        self.workflow = self.workflow_fixture()
        self.write_draft(self.workflow)
        self.write_publication(self.workflow)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"][self.task_id] = self.task_fixture(self.workflow)
        self.store.save(state)
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        if self.previous_repository_root is None:
            flask_app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            flask_app.config[
                "STRUCTURAL_REPAIR_REPOSITORY_ROOT"
            ] = self.previous_repository_root
        self.temporary.cleanup()

    @staticmethod
    def workflow_fixture():
        nodes = {}
        for step in range(1, 6):
            destination = f"step_{step + 1}" if step < 5 else "done"
            nodes[f"step_{step}"] = {
                "type": "question",
                "question": f"Check {step}?",
                "answers": {"yes": {"label": "Yes", "next": destination}},
            }
        nodes["done"] = {
            "type": "resolution",
            "title": "Done",
            "message": "Review complete.",
        }
        return {
            "workflow_id": "policy_progress",
            "name": "Policy Progress Fixture",
            "category": "Test",
            "platform": "Cross-platform",
            "estimated_steps": 4,
            "start_node": "step_1",
            "nodes": nodes,
        }

    @classmethod
    def task_fixture(cls, workflow):
        fingerprint = StructuralRepairFingerprint.semantic_workflow(workflow)
        return {
            "task_id": cls.task_id,
            "finding_id": "CUR-POLICY-PROGRESS",
            "status": "open",
            "classification": "Risk",
            "curator_rule": "CUR-WR-PROGRESS",
            "finding_type": "workflow_reasoning_progress_inconsistency",
            "content_type": "workflow",
            "content_identifier": "policy_progress",
            "structured_evidence": {
                "configured_steps": 4,
                "maximum_user_visible_nodes": 6,
            },
            "current_verification": {
                "status": "still_detected",
                "rule": "CUR-WR-PROGRESS",
                "workflow_id": "policy_progress",
                "affected_fingerprint": fingerprint,
                "verified_at": "2026-08-26T11:00:00+00:00",
            },
        }

    @staticmethod
    def encoded(value):
        return (json.dumps(value, indent=4, ensure_ascii=False) + "\n").encode("utf-8")

    def write_draft(self, workflow):
        (self.drafts / "policy_progress.json").write_bytes(self.encoded(workflow))

    def write_publication(self, workflow):
        content_hash = hashlib.sha256(json.dumps(
            workflow, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        snapshot = {
            "publication": {
                "version": 1,
                "label": "Version 1",
                "source_filename": "policy_progress.json",
                "content_hash": content_hash,
            },
            "workflow": workflow,
        }
        (self.publications / "v0001.json").write_bytes(self.encoded(snapshot))
        (self.publications / "current.json").write_bytes(self.encoded({
            "workflow_id": "policy_progress",
            "current_version": 1,
            "content_hash": content_hash,
        }))

    def update_task(self, mutate):
        state = self.store.load()
        mutate(state["tasks"][self.task_id])
        self.store.save(state)

    def set_workflow(self, workflow, *, draft=True, publication=True, verification=True):
        if draft:
            self.write_draft(workflow)
        if publication:
            self.write_publication(workflow)
        if verification:
            self.update_task(lambda task: task["current_verification"].update({
                "affected_fingerprint": StructuralRepairFingerprint.semantic_workflow(workflow),
            }))

    def evaluator(self, **kwargs):
        return CuratorProgressAutoRepairPolicyService(
            self.root, now=lambda: self.now, **kwargs,
        )

    def tree_state(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*")) if path.is_file()
        }

    @staticmethod
    def failed_ids(result):
        return {gate["gate_id"] for gate in result["gate_results"] if not gate["passed"]}

    def test_exact_accepted_style_fixture_is_eligible_and_read_only(self):
        before = self.tree_state()

        result = self.evaluator().evaluate(self.task_id).to_dict()

        self.assertTrue(result["eligible"])
        self.assertEqual(result["status"], "ELIGIBLE")
        self.assertEqual(result["policy_id"],
                         "cur-wr-progress-draft-auto-apply-eligibility")
        self.assertEqual(result["policy_version"], 2)
        self.assertEqual(len(result["gate_results"]), 21)
        self.assertTrue(all(item["passed"] for item in result["gate_results"]))
        self.assertEqual(result["failed_gate_reasons"], ())
        self.assertEqual(result["proposed_mutation"]["path"], "/progress_mode")
        self.assertEqual(result["proposed_mutation"]["after_value"], "branch_aware")
        self.assertTrue(result["proposed_mutation"]["estimated_steps_unchanged"])
        self.assertTrue(result["proposed_mutation"]["workflow_graph_unchanged"])
        self.assertEqual(result["timestamp"], self.now.isoformat())
        self.assertEqual(self.tree_state(), before)
        self.assertFalse((self.root / "curation_memory/structural_repair_approvals").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_applications").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_recoveries").exists())
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")
        registration = self.evaluator().registry.lookup(
            "CUR-WR-PROGRESS", "workflow_reasoning_progress_inconsistency"
        )
        self.assertFalse(registration.executable)

    def test_task_page_renders_eligible_policy_observation_without_mutation(self):
        before = self.tree_state()

        response = self.client.get(f"/curator/tasks/{self.task_id}")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Automation eligibility", page)
        self.assertIn("ELIGIBLE FOR FUTURE AUTO-REPAIR", page)
        self.assertIn("cur-wr-progress-draft-auto-apply-eligibility", page)
        self.assertIn("/progress_mode", page)
        self.assertIn("absent", page)
        self.assertIn("branch_aware", page)
        self.assertIn("21 passed", page)
        self.assertIn("0 failed", page)
        self.assertIn("Observation only.", page)
        self.assertIn("No automatic repair authority is enabled.", page)
        self.assertNotIn("Apply automatic repair", page)
        self.assertNotIn("Enable auto-apply", page)
        self.assertEqual(self.tree_state(), before)
        self.assertFalse((self.root / "curation_memory/structural_repair_approvals").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_applications").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_recoveries").exists())
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")

    def test_task_page_renders_ineligible_reason_and_omits_unsupported_policy(self):
        self.update_task(lambda task: task["current_verification"].update({
            "status": "appears_corrected",
        }))

        response = self.client.get(f"/curator/tasks/{self.task_id}")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("INELIGIBLE", page)
        self.assertIn("Why this task is ineligible", page)
        self.assertIn(
            "A current still_detected verification with the exact draft fingerprint is unavailable.",
            page,
        )
        self.assertIn("20 passed", page)
        self.assertIn("1 failed", page)

        self.update_task(lambda task: task.update({
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
        }))
        response = self.client.get(f"/curator/tasks/{self.task_id}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertNotIn("Automation eligibility", page)
        self.assertNotIn("cur-wr-progress-draft-auto-apply-eligibility", page)

    def test_verification_publication_and_fingerprint_failures_fail_closed(self):
        cases = []

        self.update_task(lambda task: task["current_verification"].update({
            "status": "appears_corrected",
        }))
        cases.append(("verification", self.evaluator().evaluate(self.task_id).to_dict(), "03"))
        self.setUp_from_clean()
        (self.publications / "current.json").unlink()
        cases.append(("publication", self.evaluator().evaluate(self.task_id).to_dict(), "06"))
        self.setUp_from_clean()
        self.update_task(lambda task: task["current_verification"].update({
            "affected_fingerprint": "0" * 64,
        }))
        cases.append(("fingerprint", self.evaluator().evaluate(self.task_id).to_dict(), "03"))

        for label, result, gate in cases:
            with self.subTest(label=label):
                self.assertFalse(result["eligible"])
                self.assertIn(gate, self.failed_ids(result))

        self.setUp_from_clean()
        publication_root = self.root / "app" / "workflow_publications"
        shutil.rmtree(publication_root)
        result = self.evaluator().evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("06", self.failed_ids(result))
        self.assertIn("08", self.failed_ids(result))
        self.assertFalse(publication_root.exists())

    def setUp_from_clean(self):
        self.write_draft(self.workflow)
        self.write_publication(self.workflow)
        state = self.store.load()
        state["tasks"][self.task_id] = self.task_fixture(self.workflow)
        self.store.save(state)

    def test_unrelated_metadata_and_graph_content_drift_fail_closed(self):
        metadata_drift = copy.deepcopy(self.workflow)
        metadata_drift["description"] = "Unpublished unrelated change"
        self.set_workflow(metadata_drift, publication=False)
        result = self.evaluator().evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("06", self.failed_ids(result))
        self.assertIn("08", self.failed_ids(result))

        self.setUp_from_clean()
        graph_drift = copy.deepcopy(self.workflow)
        graph_drift["nodes"]["step_1"]["question"] = "Changed content?"
        self.set_workflow(graph_drift, publication=False)
        result = self.evaluator().evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("06", self.failed_ids(result))

    def test_current_or_unsupported_progress_modes_fail_closed(self):
        for mode in ("branch_aware", "adaptive"):
            with self.subTest(mode=mode):
                self.setUp_from_clean()
                workflow = copy.deepcopy(self.workflow)
                workflow["progress_mode"] = mode
                self.set_workflow(workflow)
                result = self.evaluator().evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertIn("09", self.failed_ids(result))

    def test_every_nonbaseline_lifecycle_state_fails_closed(self):
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        for state in (
            GOVERNED_CHANGES,
            AUTHORED_OR_UNATTRIBUTED_CHANGES,
            MIXED_CHANGES,
            AMBIGUOUS_STATE,
            NO_ACTIVE_PUBLICATION,
        ):
            with self.subTest(state=state), patch.object(
                evaluator.lifecycle_projection,
                "project",
                return_value=replace(
                    baseline,
                    lifecycle_state=state,
                    publication_review_state=NOT_READY,
                ),
            ):
                result = evaluator.evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertIn("06", self.failed_ids(result))

    def test_duplicate_draft_and_provenance_ambiguity_fail_closed(self):
        (self.drafts / "duplicate.json").write_bytes(self.encoded(self.workflow))
        result = self.evaluator().evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("05", self.failed_ids(result))
        self.assertIn("06", self.failed_ids(result))

        (self.drafts / "duplicate.json").unlink()
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        with patch.object(
            evaluator.lifecycle_projection,
            "project",
            return_value=replace(
                baseline,
                ambiguity_reasons=("Governed provenance is ambiguous.",),
            ),
        ):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("08", self.failed_ids(result))

    def test_runtime_mismatch_and_compatibility_overlay_fail_closed(self):
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        for runtime in (
            WorkflowRuntimeProjection(2, False, False),
            WorkflowRuntimeProjection(1, True, True),
        ):
            with self.subTest(runtime=runtime), patch.object(
                evaluator.lifecycle_projection,
                "project",
                return_value=replace(baseline, runtime=runtime),
            ):
                result = evaluator.evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertIn("07", self.failed_ids(result))

    def test_projection_path_fingerprint_and_concurrent_draft_changes_fail_closed(self):
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        projections = (
            replace(baseline, draft_path="app/workflow_drafts/other.json"),
            replace(baseline, draft_raw_fingerprint="0" * 64),
            replace(baseline, draft_semantic_fingerprint="1" * 64),
            replace(baseline, published_semantic_fingerprint="2" * 64),
        )
        for projected in projections:
            with self.subTest(projected=projected), patch.object(
                evaluator.lifecycle_projection, "project", return_value=projected,
            ):
                result = evaluator.evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertTrue({"05", "08"} & self.failed_ids(result))

        def change_after_projection(_workflow_id):
            changed = copy.deepcopy(self.workflow)
            changed["description"] = "Changed after lifecycle projection."
            self.write_draft(changed)
            return baseline

        with patch.object(
            evaluator.lifecycle_projection, "project", side_effect=change_after_projection,
        ):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("05", self.failed_ids(result))

    def test_semantic_and_provenance_delta_projection_fails_closed(self):
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        operation = SemanticDeltaOperation(
            "replace", "/description", "before", "after", "a" * 64, "b" * 64,
        )
        projections = (
            replace(baseline, semantic_delta=(operation,)),
            replace(baseline, governed_delta_summary=("SRX: /progress_mode",)),
            replace(
                baseline,
                authored_or_unattributed_delta_summary=("replace /description",),
            ),
        )
        for projected in projections:
            with self.subTest(projected=projected), patch.object(
                evaluator.lifecycle_projection, "project", return_value=projected,
            ):
                result = evaluator.evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertIn("08", self.failed_ids(result))

    def test_additional_projected_reasoning_defect_fails_closed(self):
        evaluator = self.evaluator()
        baseline = evaluator.lifecycle_projection.project("policy_progress")
        validation = replace(
            baseline.validation,
            reasoning_findings=baseline.validation.reasoning_findings + (
                "CUR-WR-TERMINAL-EVIDENCE:workflow_reasoning_evidence_gap:done",
            ),
        )
        with patch.object(
            evaluator.lifecycle_projection,
            "project",
            return_value=replace(baseline, validation=validation),
        ):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("14", self.failed_ids(result))

    def test_preview_validation_and_new_finding_failures_fail_closed(self):
        evaluator = self.evaluator()
        with patch.object(evaluator.registry, "preview", return_value={
            "available": False,
            "read_only": True,
            "reason": "fixture preview failure",
        }):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("13", self.failed_ids(result))

        self.setUp_from_clean()
        invalid = copy.deepcopy(self.workflow)
        invalid["nodes"]["orphan"] = {
            "type": "resolution", "title": "Orphan", "message": "Unreachable",
        }
        self.set_workflow(invalid)
        result = self.evaluator().evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("14", self.failed_ids(result))

        self.setUp_from_clean()
        evaluator = self.evaluator()
        task = self.store.load()["tasks"][self.task_id]
        raw = (self.drafts / "policy_progress.json").read_bytes()
        preview = evaluator.registry.preview(
            task,
            self.workflow,
            workflow_raw_sha256=StructuralRepairFingerprint.raw_workflow(raw),
            workflow_semantic_sha256=StructuralRepairFingerprint.semantic_workflow(self.workflow),
        )
        preview["validation"]["new_reasoning_findings"] = [{"rule": "CUR-NEW"}]
        with patch.object(evaluator.registry, "preview", return_value=preview):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("17", self.failed_ids(result))

    def test_ambiguous_application_and_wrong_policy_identity_fail_closed(self):
        record = SimpleNamespace(
            task_id=self.task_id,
            finding_id="CUR-POLICY-PROGRESS",
            workflow_id="policy_progress",
            outcome="applied",
        )
        result = self.evaluator(
            application_repository=FakeApplicationRepository((record,))
        ).evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("20", self.failed_ids(result))

        for field, value in (
            ("curator_rule", "CUR-WR-OTHER"),
            ("finding_type", "other_finding"),
        ):
            with self.subTest(field=field):
                self.setUp_from_clean()
                self.update_task(lambda task, f=field, v=value: task.update({f: v}))
                result = self.evaluator().evaluate(self.task_id).to_dict()
                self.assertFalse(result["eligible"])
                self.assertIn("01", self.failed_ids(result))

        self.setUp_from_clean()
        evaluator = self.evaluator()
        registration = SimpleNamespace(
            adapter_id="wrong_adapter", executable=False,
            supervised_apply_available=True, structural=True,
        )
        with patch.object(evaluator.registry, "lookup", return_value=registration):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("01", self.failed_ids(result))

        self.setUp_from_clean()
        evaluator = self.evaluator()
        specification = SimpleNamespace(
            specification_id="wrong-specification-v1",
            approved=True,
            metadata_path="/progress_mode",
            after_value="branch_aware",
        )
        with patch.object(
            evaluator.registry, "progress_metadata_specification",
            return_value=specification,
        ):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("01", self.failed_ids(result))

        self.setUp_from_clean()
        evaluator = self.evaluator()
        unsupported_value = SimpleNamespace(
            specification_id="branch-aware-progress-metadata-v1",
            version=1,
            curator_rule="CUR-WR-PROGRESS",
            finding_type="workflow_reasoning_progress_inconsistency",
            approved=True,
            metadata_path="/progress_mode",
            allowed_before_states=("absent", "static"),
            after_value="static",
        )
        with patch.object(
            evaluator.registry, "progress_metadata_specification",
            return_value=unsupported_value,
        ):
            result = evaluator.evaluate(self.task_id).to_dict()
        self.assertFalse(result["eligible"])
        self.assertIn("10", self.failed_ids(result))


if __name__ == "__main__":
    unittest.main()
