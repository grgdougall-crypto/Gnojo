from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.curator_stage_b_reconciliation_service import (
    CuratorEarlyConvergenceStageBReconciliationService,
    CuratorSignalRetentionStageBReconciliationService,
    CuratorStageBReconciliationService,
    CuratorTerminalEvidenceCurrentEvidenceSyncService,
    CuratorTerminalEvidenceStageBReconciliationService,
)
from app.services.curator_targeted_verification_service import (
    CuratorTargetedVerificationService,
)
from curator.__main__ import main
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryStore, LockedCuratorMemory
from curator.models import InventoryRecord
from curator.reconciliation import StageBJournalError


class CuratorStageBSignalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.write_workflow(self.workflow())
        state = self.store.load()
        state["controls"]["scheduled_runs_disabled"] = False
        state["controls"]["stage_b_scheduled_runs_disabled"] = False
        state["tasks"] = {"GKT-SIGNAL": self.task_for_current_finding()}
        self.store.save(state)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(
        *, corrected: bool = False, origin_type: str = "question",
        generic_title: str = "Additional Performance Diagnostics Required",
    ):
        cpu_next = "cpu_check" if corrected else "reduce_active_workload"
        memory_next = "memory_check" if corrected else "reduce_active_workload"
        return {
            "workflow_id": "signal_retention",
            "name": "Signal Retention Fixture",
            "category": "Performance",
            "platform": "Windows",
            "start_node": "identify_bottleneck",
            "nodes": {
                "identify_bottleneck": {
                    "type": origin_type,
                    "question": "Which resource is constrained?",
                    "instruction": "Choose the constrained resource.",
                    "answers": {
                        "cpu": {"label": "CPU", "next": cpu_next},
                        "memory": {"label": "Memory", "next": memory_next},
                    },
                },
                "reduce_active_workload": {
                    "type": "instruction", "title": "Reduce Active Workload",
                    "instruction": "Close unnecessary applications.",
                    "next": "generic_result",
                },
                "generic_result": {"type": "resolution", "title": generic_title},
                "cpu_check": {
                    "type": "instruction", "title": "Inspect CPU",
                    "instruction": "Inspect CPU usage.", "next": "cpu_done",
                },
                "memory_check": {
                    "type": "instruction", "title": "Inspect Memory",
                    "instruction": "Inspect memory usage.", "next": "memory_done",
                },
                "cpu_done": {"type": "resolution", "title": "CPU Pressure"},
                "memory_done": {"type": "resolution", "title": "Memory Pressure"},
            },
        }

    def write_workflow(self, workflow, filename="signal_retention.json"):
        directory = self.root / "app" / "workflow_drafts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(workflow), encoding="utf-8")

    def current_finding(self):
        workflow = json.loads(
            (self.root / "app/workflow_drafts/signal_retention.json").read_text(
                encoding="utf-8"
            )
        )
        record = InventoryRecord(
            "workflow", "signal_retention", "Signal Retention Fixture",
            "app/workflow_drafts/signal_retention.json", "Performance",
            "Windows", "draft", workflow,
        )
        return next(
            finding for finding in CuratorChecks(self.root).run_record(record)
            if finding.rule == "CUR-WR-SIGNAL-RETENTION"
            and finding.content_identifier == "signal_retention:identify_bottleneck"
        )

    def task_for_current_finding(self, **updates):
        finding = self.current_finding()
        value = {
            "task_id": "GKT-SIGNAL",
            "finding_id": finding.identifier,
            "durable_identity": (
                "CUR-WR-SIGNAL-RETENTION|workflow_node|"
                "signal_retention:identify_bottleneck|workflow_reasoning_signal_loss"
            ),
            "status": "open", "owner": "Workflow Designer",
            "priority": "Low", "classification": "Opportunity",
            "review_disposition": "USEFUL",
            "finding_type": "workflow_reasoning_signal_loss",
            "content_type": "workflow_node",
            "content_identifier": "signal_retention:identify_bottleneck",
            "curator_rule": "CUR-WR-SIGNAL-RETENTION",
            "related_workflows": ["signal_retention"],
            "provenance": copy.deepcopy(finding.provenance),
            "evidence": ["Original evidence snapshot"],
            "current_evidence": list(finding.evidence),
            "structured_evidence": copy.deepcopy(finding.structured_evidence),
            "times_observed": 8, "last_seen": "2026-08-26T00:00:00+00:00",
            "trend": "recurring", "knowledge_debt_score": 9,
            "history": [], "resolution_history": [],
            "resolution_package": {"status": "draft_created"},
        }
        value.update(updates)
        return value

    def service(self, **values):
        return CuratorSignalRetentionStageBReconciliationService(
            self.root,
            now=lambda: datetime(2026, 8, 28, 5, 0, tzinfo=timezone.utc),
            **values,
        )

    def replace_task(self, task):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task}
        self.store.save(state)

    def current_task(self):
        return self.store.load()["tasks"]["GKT-SIGNAL"]

    def test_current_signal_loss_refreshes_still_detected_purely(self):
        before = copy.deepcopy(self.current_task())
        with patch.object(
            CuratorTargetedVerificationService, "verify",
            side_effect=AssertionError("mutating verifier must not run"),
        ):
            result = self.service().run(task_id="GKT-SIGNAL")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "still_detected")
        self.assertEqual(
            after["current_verification"]["affected_fingerprint_scope"],
            "whole_workflow",
        )
        self.assertEqual(
            after["last_verified_fingerprint"],
            CuratorTargetedVerificationService.fingerprint(self.workflow()),
        )
        self.assert_preserved(before, after)

    def test_scheduled_execution_uses_existing_supervised_mutation_boundary(self):
        before = copy.deepcopy(self.current_task())
        result = self.service().run(
            task_id="GKT-SIGNAL", trigger_source="scheduled",
            correlation_id="COR-SCHEDULED-SIGNAL",
        )
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["current_verification"]["status"], "still_detected")
        self.assert_preserved(before, after)

    def test_distinct_downstream_handling_appears_corrected_with_new_fingerprint(self):
        first = self.service().run(task_id="GKT-SIGNAL")
        self.write_workflow(self.workflow(corrected=True))
        second = self.service().run(task_id="GKT-SIGNAL")
        after = self.current_task()
        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "appears_corrected")
        self.assertNotEqual(first.task_results[0].idempotency_key,
                            second.task_results[0].idempotency_key)
        self.assertEqual(len(after["history"]), 2)

    def test_downstream_only_route_change_changes_whole_workflow_fingerprint(self):
        first = self.service().run(task_id="GKT-SIGNAL")
        workflow = self.workflow()
        origin_before = copy.deepcopy(workflow["nodes"]["identify_bottleneck"])
        workflow["nodes"]["reduce_active_workload"]["next"] = "second_generic"
        workflow["nodes"]["second_generic"] = {
            "type": "resolution", "title": "Deeper Performance Review Required",
        }
        self.write_workflow(workflow)
        second = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertEqual(
            self.current_task()["current_verification"]["status"], "still_detected"
        )
        self.assertEqual(workflow["nodes"]["identify_bottleneck"], origin_before)
        self.assertNotEqual(first.task_results[0].idempotency_key,
                            second.task_results[0].idempotency_key)

    def test_terminal_title_semantic_change_changes_whole_workflow_fingerprint(self):
        first = self.service().run(task_id="GKT-SIGNAL")
        self.write_workflow(self.workflow(
            generic_title="CPU and Memory Workload Reviewed"
        ))
        second = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertEqual(
            self.current_task()["current_verification"]["status"],
            "appears_corrected",
        )
        self.assertNotEqual(first.task_results[0].idempotency_key,
                            second.task_results[0].idempotency_key)

    def test_missing_workflow_node_nonquestion_and_duplicate_drafts_skip(self):
        path = self.root / "app/workflow_drafts/signal_retention.json"
        path.unlink()
        self.assertEqual(
            self.service().run(task_id="GKT-SIGNAL").task_results[0].status,
            "SKIPPED",
        )
        workflow = self.workflow()
        workflow["nodes"].pop("identify_bottleneck")
        self.write_workflow(workflow)
        self.assertEqual(
            self.service().run(task_id="GKT-SIGNAL").task_results[0].status,
            "SKIPPED",
        )
        self.write_workflow(self.workflow(origin_type="instruction"))
        nonquestion = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(nonquestion.task_results[0].status, "SKIPPED")
        self.assertIn("no longer a question", nonquestion.task_results[0].reason)
        self.write_workflow(self.workflow(), "duplicate.json")
        ambiguous = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(ambiguous.task_results[0].status, "SKIPPED")

    def test_wrong_identity_provenance_and_nonactionable_tasks_skip(self):
        cases = (
            {"curator_rule": "OTHER-RULE"},
            {"finding_type": "other_finding"},
            {"content_type": "workflow"},
            {"status": "resolved"},
            {"status": "deferred"},
            {"content_identifier": "signal_retention"},
            {"content_identifier": "signal_retention:identify_bottleneck:extra"},
            {"related_workflows": ["other_workflow"]},
            {"provenance": {"workflow_id": "other_workflow"}},
            {"provenance": {"node_id": "other_node"}},
            {"provenance": {"source_path": "other.json"}},
            {"provenance": "malformed"},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                task = self.task_for_current_finding(**updates)
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.service().run(task_id="GKT-SIGNAL")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertEqual(self.current_task(), before)

    def test_multiple_matches_and_finding_id_mismatch_fail_closed(self):
        finding = self.current_finding()
        service = self.service()
        with patch.object(service.checks, "run_record", return_value=[finding, finding]):
            multiple = service.run(task_id="GKT-SIGNAL")
        self.assertEqual(multiple.task_results[0].status, "SKIPPED")
        task = self.current_task()
        task["finding_id"] = "CUR-MISMATCH"
        self.replace_task(task)
        mismatch = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(mismatch.task_results[0].status, "SKIPPED")

    def test_same_key_does_not_rewrite_memory_or_duplicate_history(self):
        first = self.service().run(task_id="GKT-SIGNAL")
        memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        history_before = copy.deepcopy(self.current_task()["history"])
        second = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(second.task_results[0].status, "SKIPPED")
        self.assertEqual(first.task_results[0].idempotency_key,
                         second.task_results[0].idempotency_key)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(),
                         memory_before)
        self.assertEqual(self.current_task()["history"], history_before)

    def test_dry_run_reports_exact_delta_and_writes_nothing(self):
        before = self.files()
        result = self.service().run(task_id="GKT-SIGNAL", dry_run=True)
        delta = result.task_results[0].proposed_delta
        self.assertEqual(result.task_results[0].status, "DRY_RUN")
        self.assertEqual(delta["capability"], {
            "id": "cur-wr-signal-retention-verification-refresh", "version": 1,
        })
        self.assertEqual(delta["identity"]["workflow_id"], "signal_retention")
        self.assertEqual(
            delta["identity"]["originating_question_node_id"],
            "identify_bottleneck",
        )
        self.assertEqual(delta["verification_result"], "still_detected")
        self.assertEqual(
            set(delta["changed_fields"]),
            {"current_verification", "last_verified_fingerprint", "history"},
        )
        for key in (
            "task_lifecycle", "ranking_and_debt", "evidence", "trusted_content",
            "publication", "approvals", "packages", "repair_authority",
            "fix_wizard",
        ):
            self.assertTrue(delta["unchanged"][key], key)
        self.assertEqual(self.files(), before)

    def test_only_verification_fields_change_and_external_state_is_untouched(self):
        protected = []
        for relative in (
            "app/workflow_drafts/signal_retention.json",
            "app/workflow_publications/current.json",
            "knowledge_base/published/article.json",
            "curation_memory/resolution_packages/GKT-SIGNAL.json",
            "curation_memory/structural_repair_approvals/sentinel.json",
            "curation_memory/structural_repair_applications/sentinel.json",
            "curation_memory/structural_repair_recoveries/sentinel.json",
            "curation_memory/fix_sessions/sentinel.json",
        ):
            path = self.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sentinel", encoding="utf-8")
            protected.append(path)
        external_before = {str(path): path.read_bytes() for path in protected}
        task_before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-SIGNAL")
        task_after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        changed = {
            field for field in set(task_before) | set(task_after)
            if task_before.get(field) != task_after.get(field)
        }
        self.assertEqual(
            changed,
            {"current_verification", "last_verified_fingerprint", "history"},
        )
        self.assert_preserved(task_before, task_after)
        self.assertEqual(
            {str(path): path.read_bytes() for path in protected}, external_before
        )

    def test_shared_controls_lock_cas_and_crash_recovery_remain_bounded(self):
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        disabled = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(disabled.task_results[0].status, "FAILED")
        state = self.store.load()
        state["controls"]["global_disabled"] = False
        self.store.save(state)
        with self.store.locked():
            locked = self.service(lock_timeout=0.01).run(task_id="GKT-SIGNAL")
        self.assertEqual(locked.task_results[0].status, "FAILED")

        def change_owner(plan, attempt):
            if attempt == 0:
                current = self.store.load()
                current["tasks"]["GKT-SIGNAL"]["owner"] = "Human"
                self.store.save(current)

        service = self.service()
        service._before_commit = change_owner
        committed = service.run(task_id="GKT-SIGNAL")
        self.assertEqual(committed.task_results[0].status, "COMMITTED")
        self.assertEqual(self.current_task()["owner"], "Human")

        self.write_workflow({**self.workflow(), "name": "Changed for crash test"})
        before = (self.root / "curation_memory/memory.json").read_bytes()
        with patch.object(
            LockedCuratorMemory, "compare_and_swap",
            side_effect=RuntimeError("simulated crash before commit"),
        ):
            failed_before = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(failed_before.task_results[0].status, "FAILED")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

        service = self.service()
        append = service.journal.append

        def crash_after(event):
            if event.status == "COMMITTED":
                raise StageBJournalError("simulated journal interruption")
            return append(event)

        service.journal.append = crash_after
        failed_after = service.run(task_id="GKT-SIGNAL")
        self.assertEqual(failed_after.task_results[0].status, "FAILED")
        recovered = self.service().run(task_id="GKT-SIGNAL")
        self.assertEqual(recovered.task_results[0].status, "COMMITTED")
        self.assertIn("Recovered", recovered.task_results[0].reason)

    def test_explicit_cli_and_capabilities_one_through_four_are_unchanged(self):
        before = self.files()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "refresh-signal-retention-verification",
                "--repository", str(self.root), "--task-id", "GKT-SIGNAL",
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn(
            '"capability_id": "cur-wr-signal-retention-verification-refresh"',
            output.getvalue(),
        )
        self.assertEqual(self.files(), before)
        self.assertEqual(
            CuratorStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-progress-verification-refresh",
        )
        self.assertEqual(
            CuratorTerminalEvidenceStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-terminal-evidence-verification-refresh",
        )
        self.assertEqual(
            CuratorTerminalEvidenceCurrentEvidenceSyncService.CAPABILITY_ID,
            "cur-wr-terminal-evidence-current-evidence-sync",
        )
        self.assertEqual(
            CuratorEarlyConvergenceStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-early-convergence-verification-refresh",
        )
        self.assertEqual(
            CuratorSignalRetentionStageBReconciliationService.MUTATION_FIELDS,
            ("current_verification", "last_verified_fingerprint", "history"),
        )

    def assert_preserved(self, before, after):
        for field in (
            "status", "owner", "priority", "classification",
            "review_disposition", "evidence", "current_evidence",
            "structured_evidence", "last_seen", "times_observed", "trend",
            "knowledge_debt_score", "resolution_package", "finding_id",
            "provenance", "recommended_action", "resolution_history",
        ):
            self.assertEqual(after.get(field), before.get(field), field)

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
