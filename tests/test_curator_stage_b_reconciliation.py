import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.curator_stage_b_reconciliation_service import (
    CuratorStageBReconciliationService,
)
from curator.__main__ import main
from curator.memory import (
    CuratorMemoryConflictError,
    CuratorMemoryStore,
    LockedCuratorMemory,
)
from curator.models import InventoryRecord
from curator.observation_runner import CuratorObservationRunner
from curator.reconciliation import StageBJournalError
from curator.resolution import ResolutionPackageRepository


class CuratorStageBReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["controls"]["scheduled_runs_disabled"] = False
        state["controls"]["stage_b_scheduled_runs_disabled"] = False
        self.write_workflow(self.workflow())
        self.finding_id = self.progress_finding_id()
        state["tasks"] = {"GKT-PROGRESS": self.task()}
        self.store.save(state)

    def tearDown(self):
        self.temporary.cleanup()

    def task(self, **updates):
        value = {
            "task_id": "GKT-PROGRESS",
            "finding_id": self.finding_id,
            "durable_identity": "CUR-WR-PROGRESS|workflow|higher_layer_connectivity",
            "status": "open",
            "owner": "Workflow Designer",
            "priority": "Medium",
            "classification": "Opportunity",
            "review_disposition": "NOT_REVIEWED",
            "finding_type": "workflow_reasoning_progress_inconsistency",
            "content_type": "workflow",
            "content_identifier": "higher_layer_connectivity",
            "curator_rule": "CUR-WR-PROGRESS",
            "history": [],
            "resolution_history": [],
        }
        value.update(updates)
        return value

    @staticmethod
    def workflow(*, branch_aware=False, title="Higher Layer"):
        nodes = {}
        for step in range(1, 6):
            destination = f"step_{step + 1}" if step < 5 else "done"
            nodes[f"step_{step}"] = {
                "type": "question",
                "question": f"Check {step}?",
                "answers": {"yes": {"label": "Yes", "next": destination}},
            }
        nodes["done"] = {"type": "resolution", "title": "Done"}
        workflow = {
            "workflow_id": "higher_layer_connectivity",
            "name": title,
            "estimated_steps": 4,
            "start_node": "step_1",
            "nodes": nodes,
        }
        if branch_aware:
            workflow["progress_mode"] = "branch_aware"
        return workflow

    def service(self, **values):
        return CuratorStageBReconciliationService(
            self.root,
            now=lambda: datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc),
            **values,
        )

    def write_workflow(self, workflow, filename="higher_layer_connectivity.json"):
        directory = self.root / "app" / "workflow_drafts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(workflow), encoding="utf-8")

    def replace_task(self, task):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task}
        self.store.save(state)

    def test_dry_run_reports_exact_delta_and_writes_nothing(self):
        before = self.files()
        result = self.service().run(task_id="GKT-PROGRESS", dry_run=True)
        self.assertEqual(result.task_results[0].status, "DRY_RUN")
        self.assertEqual(
            set(result.task_results[0].proposed_delta),
            {
                "capability", "identity", "verification_result",
                "affected_fingerprint_scope", "whole_workflow_fingerprint",
                "current_verification", "last_verified_fingerprint",
                "history_event", "changed_fields", "idempotency_key",
                "eligibility", "unchanged",
            },
        )
        proposed = result.task_results[0].proposed_delta
        self.assertEqual(proposed["capability"], {
            "id": "cur-wr-progress-verification-refresh", "version": 1,
        })
        self.assertEqual(proposed["identity"]["finding_id"], self.finding_id)
        self.assertEqual(proposed["identity"]["content_type"], "workflow")
        self.assertEqual(proposed["verification_result"], "still_detected")
        self.assertEqual(proposed["affected_fingerprint_scope"], "whole_workflow")
        self.assertEqual(
            proposed["whole_workflow_fingerprint"],
            proposed["current_verification"]["after"]["affected_fingerprint"],
        )
        self.assertEqual(
            proposed["whole_workflow_fingerprint"],
            proposed["last_verified_fingerprint"]["after"],
        )
        self.assertTrue(all(proposed["unchanged"].values()))
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / "curation_memory/stage_b_reconciliations").exists())

    def test_still_detected_refresh_changes_only_allowlisted_task_fields(self):
        before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-PROGRESS")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "still_detected")
        self.assertEqual(
            after["current_verification"]["affected_fingerprint_scope"],
            "whole_workflow",
        )
        self.assertEqual(
            after["current_verification"]["affected_fingerprint"],
            after["last_verified_fingerprint"],
        )
        self.assertEqual(len(after["history"]), 1)
        self.assert_preserved(before, after)
        events = self.events(result.task_results[0].idempotency_key)
        self.assertEqual([event.status for event in events], ["PREPARED", "COMMITTED"])
        self.assertEqual(
            events[-1].declared_mutation_fields,
            ("current_verification", "last_verified_fingerprint", "history"),
        )

    def test_appears_corrected_refresh_keeps_task_open(self):
        self.write_workflow(self.workflow(branch_aware=True))
        before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-PROGRESS")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "appears_corrected")
        self.assertEqual(after["status"], "open")
        self.assert_preserved(before, after)

    def test_unsupported_and_non_actionable_tasks_are_skipped(self):
        for task in (
            self.task(curator_rule="OTHER-RULE"),
            self.task(status="resolved"),
        ):
            with self.subTest(task=task):
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.service().run(task_id="GKT-PROGRESS")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertEqual(self.current_task(), before)

    def test_wrong_content_type_is_skipped(self):
        self.replace_task(self.task(content_type="workflow_node"))
        before = copy.deepcopy(self.current_task())

        result = self.service().run(task_id="GKT-PROGRESS")

        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("progress workflow finding", result.task_results[0].reason)
        self.assertEqual(self.current_task(), before)

    def test_multiple_regenerated_findings_fail_closed(self):
        finding = SimpleNamespace(
            rule="CUR-WR-PROGRESS",
            content_identifier="higher_layer_connectivity",
            finding_type="workflow_reasoning_progress_inconsistency",
            identifier=self.finding_id,
        )
        service = self.service()
        with patch.object(service.checks, "run_record", return_value=[finding, finding]):
            result = service.run(task_id="GKT-PROGRESS")

        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("Multiple matching progress findings", result.task_results[0].reason)
        self.assertNotIn("current_verification", self.current_task())

    def test_regenerated_finding_identity_mismatch_fails_closed(self):
        finding = SimpleNamespace(
            rule="CUR-WR-PROGRESS",
            content_identifier="higher_layer_connectivity",
            finding_type="workflow_reasoning_progress_inconsistency",
            identifier="CUR-DIFFERENT",
        )
        service = self.service()
        with patch.object(service.checks, "run_record", return_value=[finding]):
            result = service.run(task_id="GKT-PROGRESS")

        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("finding identity", result.task_results[0].reason)
        self.assertNotIn("current_verification", self.current_task())

    def test_workflow_and_provenance_identity_mismatches_fail_closed(self):
        cases = (
            self.task(related_workflows=["different_workflow"]),
            self.task(provenance={"workflow_id": "different_workflow"}),
            self.task(provenance={"node_id": "step_1"}),
            self.task(provenance={"source_path": "different/path.json"}),
            self.task(provenance={"lifecycle": "published"}),
            self.task(provenance="malformed"),
        )
        for task in cases:
            with self.subTest(task=task):
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.service().run(task_id="GKT-PROGRESS")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertTrue(
                    "identity" in result.task_results[0].reason.lower()
                    or "provenance" in result.task_results[0].reason.lower()
                )
                self.assertEqual(self.current_task(), before)

    def test_missing_and_ambiguous_workflow_fail_closed(self):
        path = self.root / "app/workflow_drafts/higher_layer_connectivity.json"
        path.unlink()
        missing = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(missing.task_results[0].status, "SKIPPED")
        self.assertIn("unavailable", missing.task_results[0].reason)
        self.write_workflow(self.workflow(), "one.json")
        self.write_workflow(self.workflow(), "two.json")
        ambiguous = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(ambiguous.task_results[0].status, "SKIPPED")
        self.assertIn("Multiple editable", ambiguous.task_results[0].reason)
        self.assertNotIn("current_verification", self.current_task())

    def test_same_idempotency_key_does_not_rewrite_memory_or_history(self):
        first = self.service().run(task_id="GKT-PROGRESS")
        memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        history_before = copy.deepcopy(self.current_task()["history"])
        second = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(first.task_results[0].idempotency_key,
                         second.task_results[0].idempotency_key)
        self.assertEqual(second.task_results[0].status, "SKIPPED")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), memory_before)
        self.assertEqual(self.current_task()["history"], history_before)

    def test_changed_content_fingerprint_creates_one_new_verification_event(self):
        first = self.service().run(task_id="GKT-PROGRESS")
        self.write_workflow(self.workflow(branch_aware=True, title="Updated"))
        second = self.service().run(task_id="GKT-PROGRESS")
        self.assertNotEqual(first.task_results[0].idempotency_key,
                            second.task_results[0].idempotency_key)
        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertEqual(len(self.current_task()["history"]), 2)

    def test_downstream_only_change_changes_whole_workflow_fingerprint(self):
        first = self.service().run(task_id="GKT-PROGRESS")
        workflow = self.workflow()
        workflow["nodes"]["done"]["title"] = "Updated downstream outcome"
        self.write_workflow(workflow)

        second = self.service().run(task_id="GKT-PROGRESS")

        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertNotEqual(
            first.task_results[0].idempotency_key,
            second.task_results[0].idempotency_key,
        )
        self.assertNotEqual(
            first.task_results[0].proposed_delta["whole_workflow_fingerprint"],
            second.task_results[0].proposed_delta["whole_workflow_fingerprint"],
        )
        self.assertEqual(len(self.current_task()["history"]), 2)

    def test_trusted_content_and_governance_artifacts_are_unchanged(self):
        package = ResolutionPackageRepository(self.root / "curation_memory").save({
            "task_id": "GKT-PROGRESS", "status": "draft_created",
            "recommendation": "CREATE_NEW_ARTICLE",
        })
        del package
        protected = [
            self.root / "app/workflow_drafts/higher_layer_connectivity.json",
            self.root / "curation_memory/resolution_packages/GKT-PROGRESS.json",
        ]
        for relative in (
            "app/workflow_publications/current.json",
            "curation_memory/structural_repair_approvals/sentinel.json",
            "curation_memory/structural_repair_applications/sentinel.json",
            "curation_memory/structural_repair_recoveries/sentinel.json",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("sentinel", encoding="utf-8")
            protected.append(path)
        before = {str(path): path.read_bytes() for path in protected}
        self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual({str(path): path.read_bytes() for path in protected}, before)

    def test_global_and_scheduled_controls_block_without_task_mutation(self):
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        before = copy.deepcopy(self.current_task())
        global_result = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(global_result.task_results[0].status, "FAILED")
        self.assertEqual(self.current_task(), before)
        state = self.store.load()
        state["controls"]["global_disabled"] = False
        state["controls"]["scheduled_runs_disabled"] = True
        self.store.save(state)
        scheduled = self.service().run(
            task_id="GKT-PROGRESS", trigger_source="scheduled"
        )
        self.assertEqual(scheduled.task_results[0].status, "FAILED")
        self.assertEqual(self.current_task(), before)

    def test_scheduled_control_is_rechecked_inside_lock(self):
        service = self.service()

        def disable_scheduled(plan, attempt):
            if attempt == 0:
                state = self.store.load()
                state["controls"]["scheduled_runs_disabled"] = True
                self.store.save(state)

        service._before_commit = disable_scheduled
        result = service.run(
            task_id="GKT-PROGRESS", trigger_source="scheduled"
        )
        self.assertEqual(result.task_results[0].status, "FAILED")
        self.assertIn("Scheduled Curator runs are disabled", result.task_results[0].reason)
        self.assertNotIn("current_verification", self.current_task())

    def test_stage_b_scheduled_control_is_rechecked_inside_lock(self):
        service = self.service()

        def disable_stage_b(plan, attempt):
            if attempt == 0:
                state = self.store.load()
                state["controls"]["stage_b_scheduled_runs_disabled"] = True
                self.store.save(state)

        service._before_commit = disable_stage_b
        result = service.run(
            task_id="GKT-PROGRESS", trigger_source="scheduled"
        )
        self.assertEqual(result.task_results[0].status, "FAILED")
        self.assertIn("Scheduled Stage B runs are disabled", result.task_results[0].reason)
        self.assertNotIn("current_verification", self.current_task())

    def test_manual_execution_ignores_stage_b_scheduled_control(self):
        state = self.store.load()
        state["controls"]["stage_b_scheduled_runs_disabled"] = True
        self.store.save(state)
        result = self.service().run(task_id="GKT-PROGRESS", trigger_source="manual")
        self.assertEqual(result.task_results[0].status, "COMMITTED")

    def test_shared_lock_overlap_fails_closed(self):
        with self.store.locked():
            result = self.service(lock_timeout=0.01).run(task_id="GKT-PROGRESS")
        self.assertEqual(result.task_results[0].status, "FAILED")
        self.assertNotIn("current_verification", self.current_task())

    def test_cas_recompute_preserves_concurrent_human_change(self):
        service = self.service()
        calls = []

        def change_owner(plan, attempt):
            calls.append(attempt)
            if attempt == 0:
                state = self.store.load()
                state["tasks"]["GKT-PROGRESS"]["owner"] = "Human"
                self.store.save(state)

        service._before_commit = change_owner
        result = service.run(task_id="GKT-PROGRESS")
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(self.current_task()["owner"], "Human")
        self.assertEqual(calls, [0, 1])

    def test_stale_store_save_cannot_overwrite_concurrent_human_change(self):
        stale = self.store.load()
        current = CuratorMemoryStore(self.root / "curation_memory").load()
        current["tasks"]["GKT-PROGRESS"]["owner"] = "Human"
        CuratorMemoryStore(self.root / "curation_memory").save(current)
        stale["tasks"]["GKT-PROGRESS"]["priority"] = "High"
        with self.assertRaises(CuratorMemoryConflictError):
            self.store.save(stale)
        task = self.current_task()
        self.assertEqual(task["owner"], "Human")
        self.assertEqual(task["priority"], "Medium")

    def test_crash_before_commit_leaves_memory_unchanged_and_retry_is_safe(self):
        before = (self.root / "curation_memory/memory.json").read_bytes()
        with patch.object(
            LockedCuratorMemory, "compare_and_swap",
            side_effect=RuntimeError("simulated crash before commit"),
        ):
            failed = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(failed.task_results[0].status, "FAILED")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)
        self.assertEqual(
            [event.status for event in self.events(failed.task_results[0].idempotency_key)],
            ["PREPARED", "FAILED"],
        )
        retry = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(retry.task_results[0].status, "COMMITTED")
        self.assertEqual(len(self.current_task()["history"]), 1)

    def test_crash_after_commit_is_recovered_without_duplicate_history(self):
        service = self.service()
        append = service.journal.append

        def crash(event):
            if event.status == "COMMITTED":
                raise StageBJournalError("simulated journal interruption")
            return append(event)

        service.journal.append = crash
        failed = service.run(task_id="GKT-PROGRESS")
        self.assertEqual(failed.task_results[0].status, "FAILED")
        self.assertEqual(len(self.current_task()["history"]), 1)
        recovered = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(recovered.task_results[0].status, "COMMITTED")
        self.assertIn("Recovered", recovered.task_results[0].reason)
        self.assertEqual(len(self.current_task()["history"]), 1)
        self.assertEqual(
            [event.status for event in self.events(recovered.task_results[0].idempotency_key)],
            ["PREPARED", "FAILED", "COMMITTED"],
        )

    def test_corrupt_journal_blocks_execution(self):
        corrupt = self.root / "curation_memory/stage_b_reconciliations/not-valid"
        corrupt.mkdir(parents=True)
        (corrupt / "bad.json").write_text("{}", encoding="utf-8")
        before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-PROGRESS")
        self.assertEqual(result.task_results[0].status, "FAILED")
        self.assertEqual(self.current_task(), before)

    def test_stage_a_observation_remains_read_only_and_compatible(self):
        before = (self.root / "curation_memory/memory.json").read_bytes()
        result = CuratorObservationRunner(
            self.root, results_root=self.root / "curation_observations"
        ).run("health")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_bounded_cli_dry_run_exposes_only_the_allowlisted_capability(self):
        before = self.files()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "refresh-progress-verification", "--repository", str(self.root),
                "--task-id", "GKT-PROGRESS", "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn('"capability_id": "cur-wr-progress-verification-refresh"',
                      output.getvalue())
        self.assertEqual(self.files(), before)

    def current_task(self):
        return self.store.load()["tasks"]["GKT-PROGRESS"]

    def progress_finding_id(self):
        service = self.service()
        target = service.lifecycle.resolve("higher_layer_connectivity")
        workflow = target.workflow
        record = InventoryRecord(
            "workflow", "higher_layer_connectivity",
            str(workflow.get("name") or "higher_layer_connectivity"),
            target.source_path, str(workflow.get("category") or ""),
            str(workflow.get("platform") or ""), target.lifecycle, workflow,
        )
        findings = [
            finding for finding in service.checks.run_record(record)
            if finding.rule == "CUR-WR-PROGRESS"
        ]
        self.assertEqual(len(findings), 1)
        return findings[0].identifier

    def events(self, key):
        return self.service().journal.get(key)

    def assert_preserved(self, before, after):
        for field in (
            "status", "owner", "priority", "classification", "review_disposition",
            "resolution_history", "finding_id", "task_id", "current_evidence",
            "structured_evidence", "times_observed", "last_seen", "trend",
            "knowledge_debt_score", "disposition",
        ):
            self.assertEqual(after.get(field), before.get(field), field)

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
