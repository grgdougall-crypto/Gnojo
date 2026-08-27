from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from app.services.curator_stage_b_reconciliation_service import (
    CuratorEarlyConvergenceStageBReconciliationService,
    CuratorSignalRetentionStageBReconciliationService,
    StageBRunResult,
    StageBTaskPlan,
    StageBTaskResult,
)
from curator.memory import CuratorMemoryStore
from curator.__main__ import main
from curator.reconciliation import StageBJournalEvent, StageBJournalRepository
from curator.stage_b_scheduled_repository import StageBScheduledRunRepository
from curator.stage_b_scheduled_runner import (
    CuratorStageBScheduledRunner,
    StageBScheduledRunnerError,
)


class CuratorStageBScheduledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["controls"].update({
            "global_disabled": False,
            "scheduled_runs_disabled": False,
            "stage_b_scheduled_runs_disabled": False,
        })
        self.store.save(state)
        self.now = lambda: datetime(2026, 8, 28, 20, 15, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def task(task_id: str, capability: int, **updates):
        if capability == 4:
            rule = "CUR-WR-EARLY-CONVERGENCE"
            finding_type = "workflow_reasoning_early_convergence"
        else:
            rule = "CUR-WR-SIGNAL-RETENTION"
            finding_type = "workflow_reasoning_signal_loss"
        value = {
            "task_id": task_id,
            "finding_id": f"CUR-{task_id}",
            "status": "open",
            "curator_rule": rule,
            "finding_type": finding_type,
            "content_type": "workflow_node",
        }
        value.update(updates)
        return value

    def set_tasks(self, *tasks):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task for task in tasks}
        self.store.save(state)

    @staticmethod
    def plan(task_id: str, *, eligible: bool = True, marker: str = ""):
        key = hashlib.sha256(f"{task_id}|{marker}".encode()).hexdigest()
        return StageBTaskPlan(
            task_id=task_id,
            finding_id=f"CUR-{task_id}",
            eligible=eligible,
            reason="Eligible fixture." if eligible else "Ineligible fixture.",
            verification_status="still_detected" if eligible else "SKIPPED",
            affected_fingerprint="a" * 64 if eligible else "",
            idempotency_key=key,
            precondition_fingerprint="b" * 64,
            before_task_fingerprint="c" * 64,
            after_task_fingerprint="d" * 64,
            declared_mutation_fields=("current_verification",),
            proposed_delta={"task_id": task_id},
            state_after={} if eligible else None,
        )

    @staticmethod
    def service_result(service_type, task_id: str, status: str = "COMMITTED"):
        return StageBRunResult(
            run_id=f"STB-{task_id}",
            correlation_id="COR-SCHEDULED",
            capability_id=service_type.CAPABILITY_ID,
            capability_version=1,
            dry_run=False,
            task_results=(StageBTaskResult(
                task_id, status, f"{status} fixture.",
                hashlib.sha256(task_id.encode()).hexdigest(), {},
            ),),
        )

    def runner(self):
        return CuratorStageBScheduledRunner(self.root, now=self.now)

    def test_allowlist_is_exactly_capabilities_four_and_five(self):
        self.assertEqual(
            [(item.capability_id, item.capability_version)
             for item in self.runner().ALLOWLIST],
            [
                ("cur-wr-early-convergence-verification-refresh", 1),
                ("cur-wr-signal-retention-verification-refresh", 1),
            ],
        )

    def test_cli_is_bounded_and_persists_a_zero_candidate_summary(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "stage-b-scheduled", "--repository", str(self.root),
                "--correlation-id", "COR-CLI", "--max-candidates", "3",
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "SUCCEEDED_NO_CHANGES")
        self.assertEqual(payload["correlation_id"], "COR-CLI")
        self.assertIsNotNone(
            StageBScheduledRunRepository(
                self.root / "curation_memory"
            ).get(payload["runner_id"])
        )

        with self.assertRaises(SystemExit):
            main(["stage-b-scheduled", "--task-id", "GKT-FORBIDDEN"])

    def test_discovery_filters_and_round_robin_are_deterministic(self):
        self.set_tasks(
            self.task("GKT-E2", 4), self.task("GKT-S2", 5),
            self.task("GKT-E1", 4), self.task("GKT-S1", 5),
            self.task("GKT-CLOSED", 4, status="resolved"),
            self.task("GKT-WRONG", 4, content_type="workflow"),
            {"task_id": "GKT-CAP1", "status": "open",
             "curator_rule": "CUR-WR-PROGRESS",
             "finding_type": "workflow_reasoning_progress_inconsistency",
             "content_type": "workflow"},
        )
        queues = self.runner()._discover(self.store.load())
        order = [task_id for _, task_id in self.runner()._round_robin(queues)]
        self.assertEqual(order, ["GKT-E1", "GKT-S1", "GKT-E2", "GKT-S2"])
        self.assertNotIn("GKT-CAP1", order)

    def test_max_five_and_ineligible_candidate_does_not_consume_limit(self):
        tasks = [self.task(f"GKT-E{index}", 4) for index in range(1, 8)]
        self.set_tasks(*tasks)
        executed = []

        def preflight(_service, task_id):
            return self.plan(task_id, eligible=task_id != "GKT-E1")

        def execute(service, *, task_id, **_values):
            executed.append(task_id)
            return self.service_result(type(service), task_id)

        with patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "plan_task", autospec=True, side_effect=preflight,
        ), patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "run", autospec=True, side_effect=execute,
        ):
            result = self.runner().run(max_candidates=5)

        self.assertEqual(executed, ["GKT-E2", "GKT-E3", "GKT-E4", "GKT-E5", "GKT-E6"])
        self.assertEqual(result.summary["preflight_skipped_count"], 1)
        self.assertEqual(result.summary["committed_count"], 5)
        self.assertEqual(result.status, "SUCCEEDED")

    def test_committed_noop_and_zero_candidates_create_no_task_journal_noise(self):
        self.set_tasks(self.task("GKT-E1", 4))
        plan = self.plan("GKT-E1")
        journal = StageBJournalRepository(self.root / "curation_memory")
        prepared = self.journal_event(plan, "PREPARED")
        journal.append(prepared)
        journal.append(self.journal_event(plan, "COMMITTED", previous=prepared))
        before = list((self.root / "curation_memory/stage_b_reconciliations").rglob("*.json"))

        with patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "plan_task", return_value=plan,
        ), patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "run", side_effect=AssertionError("committed no-op must not execute"),
        ):
            result = self.runner().run()
        after = list((self.root / "curation_memory/stage_b_reconciliations").rglob("*.json"))
        self.assertEqual(result.status, "SUCCEEDED_NO_CHANGES")
        self.assertEqual(result.summary["committed_no_op_count"], 1)
        self.assertEqual(len(after), len(before))

        self.set_tasks()
        zero = self.runner().run()
        self.assertEqual(zero.status, "SUCCEEDED_NO_CHANGES")
        self.assertEqual(zero.summary["discovered_count"], 0)
        self.assertEqual(len(list((self.root / "curation_memory/stage_b_reconciliations").rglob("*.json"))), len(after))

    def test_prepared_recovery_is_not_prefiltered(self):
        self.set_tasks(self.task("GKT-E1", 4))
        plan = self.plan("GKT-E1")
        StageBJournalRepository(self.root / "curation_memory").append(
            self.journal_event(plan, "PREPARED")
        )
        calls = []
        with patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "plan_task", return_value=plan,
        ), patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "run", autospec=True,
            side_effect=lambda service, **values: (
                calls.append(values["task_id"])
                or self.service_result(type(service), values["task_id"])
            ),
        ):
            result = self.runner().run()
        self.assertEqual(calls, ["GKT-E1"])
        self.assertEqual(result.summary["committed_count"], 1)

    def test_capabilities_four_and_five_receive_scheduled_authority(self):
        self.set_tasks(self.task("GKT-E1", 4), self.task("GKT-S1", 5))
        seen = []

        def plan(_service, task_id):
            return self.plan(task_id)

        def execute(service, **values):
            seen.append((type(service).CAPABILITY_ID, values))
            return self.service_result(type(service), values["task_id"])

        with patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "plan_task", autospec=True, side_effect=plan,
        ), patch.object(
            CuratorSignalRetentionStageBReconciliationService,
            "plan_task", autospec=True, side_effect=plan,
        ), patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "run", autospec=True, side_effect=execute,
        ), patch.object(
            CuratorSignalRetentionStageBReconciliationService,
            "run", autospec=True, side_effect=execute,
        ):
            result = self.runner().run(correlation_id="COR-SCHEDULED")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(values["trigger_source"] == "scheduled" for _, values in seen))
        self.assertTrue(all(values["correlation_id"] == "COR-SCHEDULED" for _, values in seen))

    def test_dry_run_writes_nothing(self):
        self.set_tasks(self.task("GKT-E1", 4))
        before = self.files()
        with patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "plan_task", return_value=self.plan("GKT-E1"),
        ), patch.object(
            CuratorEarlyConvergenceStageBReconciliationService,
            "run", side_effect=AssertionError("dry run must not execute"),
        ):
            result = self.runner().run(dry_run=True)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.summary["per_capability_counts"][
            "cur-wr-early-convergence-verification-refresh"
        ]["would_execute"], 1)
        self.assertEqual(self.files(), before)

    def test_independent_control_defaults_disabled_and_stage_a_alone_cannot_authorize(self):
        root = Path(tempfile.mkdtemp(dir=self.root))
        store = CuratorMemoryStore(root / "curation_memory")
        state = store.load()
        self.assertTrue(state["controls"]["stage_b_scheduled_runs_disabled"])
        state["controls"]["scheduled_runs_disabled"] = False
        store.save(state)
        before = self.files_under(root)
        with self.assertRaisesRegex(StageBScheduledRunnerError, "Stage B runs are disabled"):
            CuratorStageBScheduledRunner(root).run()
        self.assertEqual(self.files_under(root), before)

    def test_corrupt_journal_blocks_before_runner_record_or_task_mutation(self):
        self.set_tasks(self.task("GKT-E1", 4))
        invalid = self.root / "curation_memory/stage_b_reconciliations/invalid"
        invalid.mkdir(parents=True)
        before = self.files()
        with self.assertRaisesRegex(StageBScheduledRunnerError, "invalid identity"):
            self.runner().run()
        self.assertEqual(self.files(), before)
        self.assertFalse(
            (self.root / "curation_memory/stage_b_scheduled_runs").exists()
        )

    def test_real_run_persists_running_then_final_and_interrupted_stays_visible(self):
        self.set_tasks()
        repository = StageBScheduledRunRepository(self.root / "curation_memory")
        observed = []
        original_finalize = repository.finalize

        def finalize(runner_id, value):
            observed.append(repository.get(runner_id)["status"])
            return original_finalize(runner_id, value)

        runner = self.runner()
        runner.results = repository
        with patch.object(repository, "finalize", side_effect=finalize):
            result = runner.run()
        self.assertEqual(observed, ["RUNNING"])
        self.assertEqual(repository.get(result.runner_id)["status"], "SUCCEEDED_NO_CHANGES")

        interrupted = runner._record("STBS-INTERRUPTED", "COR-INTERRUPTED", "RUNNING", runner._empty_summary(runner._discover(self.store.load())))
        repository.create_running(interrupted)
        self.assertEqual(repository.latest()["status"], "RUNNING")

    def test_failure_stops_remaining_candidates_and_reports_partial_failure(self):
        self.set_tasks(self.task("GKT-E1", 4), self.task("GKT-S1", 5), self.task("GKT-E2", 4))
        executed = []

        def plan(_service, task_id):
            return self.plan(task_id)

        def early(service, **values):
            executed.append(values["task_id"])
            return self.service_result(type(service), values["task_id"])

        def signal(service, **values):
            executed.append(values["task_id"])
            return self.service_result(type(service), values["task_id"], "FAILED")

        with patch.object(CuratorEarlyConvergenceStageBReconciliationService, "plan_task", autospec=True, side_effect=plan), patch.object(CuratorSignalRetentionStageBReconciliationService, "plan_task", autospec=True, side_effect=plan), patch.object(CuratorEarlyConvergenceStageBReconciliationService, "run", autospec=True, side_effect=early), patch.object(CuratorSignalRetentionStageBReconciliationService, "run", autospec=True, side_effect=signal):
            result = self.runner().run()
        self.assertEqual(result.status, "PARTIAL_FAILED")
        self.assertEqual(executed, ["GKT-E1", "GKT-S1"])
        self.assertEqual(result.summary["failed_count"], 1)

    def journal_event(self, plan, status, previous=None):
        return StageBJournalEvent.build(
            previous=previous, event_id=f"SBE-{status}-{plan.task_id}",
            run_id="STB-FIXTURE", correlation_id="COR-FIXTURE",
            capability_id="cur-wr-early-convergence-verification-refresh",
            capability_version=1, task_id=plan.task_id,
            finding_id=plan.finding_id, idempotency_key=plan.idempotency_key,
            precondition_fingerprint=plan.precondition_fingerprint,
            before_task_fingerprint=plan.before_task_fingerprint,
            after_task_fingerprint=plan.after_task_fingerprint,
            declared_mutation_fields=plan.declared_mutation_fields,
            at="2026-08-28T20:15:00+00:00", status=status,
            reason=f"{status} fixture.",
        )

    def files(self):
        return self.files_under(self.root)

    @staticmethod
    def files_under(root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
