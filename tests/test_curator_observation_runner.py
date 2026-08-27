import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from curator.__main__ import main
from curator.memory import CuratorMemoryStore
from curator.observation_models import (
    FAILED,
    PURE_OBSERVATION,
    RUNNING,
    SKIPPED_OVERLAP,
    SUCCEEDED,
    ObservationPayload,
    ObservationRunResult,
)
from curator.observation_repository import (
    ObservationLock,
    ObservationOverlapError,
    ObservationResultRepository,
)
from curator.observation_runner import CuratorObservationRunner, ObservationRunnerError


class CuratorObservationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in (
            "app/workflow_drafts",
            "app/workflow_publications",
            "app/decision_trees",
            "app/troubleshooting_history",
            "knowledge_base",
            "curation_memory",
        ):
            (self.root / relative).mkdir(parents=True)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.state = self.store.load()
        self.state["controls"]["scheduled_runs_disabled"] = True
        self.store.save(self.state)
        self.results_root = self.root / "observation-results"
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self):
        return CuratorObservationRunner(
            self.root,
            results_root=self.results_root,
            now=lambda: self.now,
        )

    def test_all_allowlisted_jobs_route_as_pure_observation(self):
        runner = self.runner()
        for job in runner.JOBS:
            runner.handlers[job] = lambda job=job: ObservationPayload(
                observation_counts=((job, 1),)
            )
            result = runner.run(job)
            self.assertEqual(result.status, SUCCEEDED)
            self.assertEqual(result.execution_class, PURE_OBSERVATION)
            self.assertFalse(result.trusted_content_changed)
            self.assertFalse(result.curator_state_changed)

        with self.assertRaises(ObservationRunnerError):
            runner.run("unknown")
        with self.assertRaises(ObservationRunnerError):
            runner.run("health", execution_class="CURATOR_RECONCILIATION")

    def test_every_real_job_leaves_application_state_unchanged(self):
        before = self.application_state()
        results = [self.runner().run(job) for job in self.runner().JOBS]
        after = self.application_state()

        self.assertTrue(all(item.status == SUCCEEDED for item in results), results)
        self.assertEqual(after, before)
        self.assertFalse((self.root / ".curator-observation.lock").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_approvals").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_applications").exists())
        self.assertFalse((self.root / "curation_memory/structural_repair_recoveries").exists())

    def test_audit_explicitly_uses_write_false(self):
        audit_result = Mock()
        audit_result.inventory = []
        audit_result.findings = []
        audit_result.coverage = {}
        audit_result.summary.return_value = {
            "findings_by_classification": {},
        }
        auditor = Mock()
        auditor.audit.return_value = (audit_result, None)
        with patch(
            "curator.observation_runner.CuratorAuditor", return_value=auditor
        ):
            result = self.runner().run("audit")
        self.assertEqual(result.status, SUCCEEDED)
        auditor.audit.assert_called_once_with(write=False)

    def test_analytics_explicitly_uses_production_scope_and_not_bridge(self):
        history = Mock()
        history.list.return_value = []
        with patch(
            "curator.observation_runner.TroubleshootingHistoryService",
            return_value=history,
        ), patch(
            "curator.observation_runner.ContentQualityService.build",
            return_value={"action_queue": []},
        ) as build:
            result = self.runner().run("analytics")
        self.assertEqual(result.status, SUCCEEDED)
        history.list.assert_called_once_with(500, environment="production")
        build.assert_called_once()

    def test_result_repository_persists_running_success_failure_and_lists_recent(self):
        repository = ObservationResultRepository(self.results_root)
        running = self.result("OBS-RUNNING", RUNNING, completed_at="")
        repository.create(running)
        self.assertEqual(repository.get("OBS-RUNNING").status, RUNNING)

        succeeded = self.result("OBS-SUCCESS", SUCCEEDED)
        failed = self.result("OBS-FAILED", FAILED, errors=("Sanitized failure.",))
        repository.create(succeeded)
        repository.create(failed)
        self.assertEqual(repository.get("OBS-SUCCESS"), succeeded)
        self.assertEqual(repository.get("OBS-FAILED"), failed)
        self.assertEqual(len(repository.list_recent()), 3)

    def test_failed_job_is_sanitized_persisted_and_releases_lock(self):
        runner = self.runner()

        def fail():
            raise RuntimeError("secret local path C:/sensitive")

        runner.handlers["health"] = fail
        result = runner.run("health")
        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.errors, ("Observation failed (RuntimeError).",))
        self.assertNotIn("sensitive", json.dumps(result.to_dict()))
        self.assertEqual(runner.results.get(result.run_id), result)
        self.assertFalse(runner.lock_path.exists())

    def test_lock_acquire_release_overlap_and_old_lock_is_not_removed(self):
        lock_path = self.root / ".curator-observation.lock"
        with ObservationLock(
            lock_path, job_type="health", run_id="OBS-OWNER", acquired_at="2000-01-01"
        ):
            self.assertTrue(lock_path.exists())
            with self.assertRaises(ObservationOverlapError):
                with ObservationLock(
                    lock_path,
                    job_type="audit",
                    run_id="OBS-SECOND",
                    acquired_at="2099-01-01",
                ):
                    pass
            self.assertEqual(json.loads(lock_path.read_text())["run_id"], "OBS-OWNER")
        self.assertFalse(lock_path.exists())

        lock_path.write_text(json.dumps({
            "pid": 999999,
            "host": "old-host",
            "job_type": "audit",
            "run_id": "OBS-STALE",
            "acquired_at": "2000-01-01T00:00:00+00:00",
        }))
        result = self.runner().run("health")
        self.assertEqual(result.status, SKIPPED_OVERLAP)
        self.assertTrue(lock_path.exists())

    def test_live_same_host_lock_is_never_removed_even_when_old(self):
        lock_path = self.root / ".curator-observation.lock"
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "job_type": "audit",
            "run_id": "OBS-LIVE-OWNER",
            "acquired_at": "2000-01-01T00:00:00+00:00",
        }
        lock_path.write_text(json.dumps(owner), encoding="utf-8")
        before = lock_path.read_bytes()
        result = self.runner().run("health")
        self.assertEqual(result.status, SKIPPED_OVERLAP)
        self.assertEqual(lock_path.read_bytes(), before)
        self.assertEqual(
            list((self.results_root / "_orphaned_locks").glob("*.lock.json")),
            [],
        )

    def test_dead_same_host_lock_recovers_and_marks_interrupted_result(self):
        lock_path = self.root / ".curator-observation.lock"
        owner_run = "OBS-ORPHANED-OWNER"
        acquired_at = "2026-08-26T11:55:00+00:00"
        owner = {
            "pid": 9552,
            "host": socket.gethostname(),
            "job_type": "health",
            "run_id": owner_run,
            "acquired_at": acquired_at,
        }
        lock_path.write_text(json.dumps(owner), encoding="utf-8")
        interrupted = self.result(owner_run, RUNNING, completed_at="")
        interrupted = ObservationRunResult(
            **{
                **interrupted.__dict__,
                "started_at": acquired_at,
            }
        )
        ObservationResultRepository(self.results_root).create(interrupted)
        with patch.object(ObservationLock, "_process_liveness", return_value=False):
            result = self.runner().run("health")
        self.assertEqual(result.status, SUCCEEDED)
        recovery_warnings = [
            item for item in result.warnings
            if "orphaned observation lock" in item
        ]
        self.assertEqual(len(recovery_warnings), 1)
        self.assertIn(owner_run, recovery_warnings[0])
        self.assertIn("health", recovery_warnings[0])
        self.assertFalse(lock_path.exists())
        evidence = list((self.results_root / "_orphaned_locks").glob("*.lock.json"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0].read_text(encoding="utf-8")), owner)
        recovered = ObservationResultRepository(self.results_root).get(owner_run)
        self.assertEqual(recovered.status, FAILED)
        self.assertTrue(any("interrupted" in item for item in recovered.warnings))

    def test_recovery_warning_is_durable_before_handler_finishes(self):
        lock_path = self.root / ".curator-observation.lock"
        owner = {
            "pid": 9552,
            "host": socket.gethostname(),
            "job_type": "audit",
            "run_id": "OBS-DURABLE-RECOVERY",
            "acquired_at": "2026-08-26T11:55:00+00:00",
        }
        lock_path.write_text(json.dumps(owner), encoding="utf-8")
        runner = self.runner()

        def inspect_running_then_fail():
            running = next(
                item for item in runner.results.list_recent()
                if item.status == RUNNING and item.run_id != owner["run_id"]
            )
            warnings = [
                item for item in running.warnings
                if "orphaned observation lock" in item
            ]
            self.assertEqual(len(warnings), 1)
            raise RuntimeError("simulated interrupted handler")

        runner.handlers["health"] = inspect_running_then_fail
        with patch.object(ObservationLock, "_process_liveness", return_value=False):
            result = runner.run("health")
        self.assertEqual(result.status, FAILED)
        warnings = [
            item for item in result.warnings
            if "orphaned observation lock" in item
        ]
        self.assertEqual(len(warnings), 1)

    def test_foreign_and_malformed_lock_ownership_fail_closed(self):
        lock_path = self.root / ".curator-observation.lock"
        cases = (
            {
                "pid": 9552,
                "host": "foreign-host",
                "job_type": "audit",
                "run_id": "OBS-FOREIGN",
                "acquired_at": "2026-08-26T00:00:00+00:00",
            },
            {
                "pid": "9552",
                "host": socket.gethostname(),
                "job_type": "audit",
                "run_id": "OBS-BAD-PID",
                "acquired_at": "2026-08-26T00:00:00+00:00",
            },
            {"pid": 9552, "host": socket.gethostname()},
        )
        for owner in cases:
            with self.subTest(owner=owner):
                lock_path.write_text(json.dumps(owner), encoding="utf-8")
                before = lock_path.read_bytes()
                with patch.object(
                    ObservationLock, "_process_liveness", return_value=False
                ):
                    result = self.runner().run("health")
                self.assertEqual(result.status, SKIPPED_OVERLAP)
                self.assertEqual(lock_path.read_bytes(), before)
                lock_path.unlink()
        lock_path.write_text("not-json", encoding="utf-8")
        malformed_before = lock_path.read_bytes()
        result = self.runner().run("health")
        self.assertEqual(result.status, SKIPPED_OVERLAP)
        self.assertEqual(lock_path.read_bytes(), malformed_before)

    def test_dead_orphan_without_matching_running_result_preserves_lock_evidence(self):
        lock_path = self.root / ".curator-observation.lock"
        owner = {
            "pid": 9552,
            "host": socket.gethostname(),
            "job_type": "audit",
            "run_id": "OBS-NO-RESULT",
            "acquired_at": "2026-08-26T00:00:00+00:00",
        }
        lock_path.write_text(json.dumps(owner), encoding="utf-8")
        with patch.object(ObservationLock, "_process_liveness", return_value=False):
            result = self.runner().run("health")
        self.assertEqual(result.status, SUCCEEDED)
        self.assertTrue(any("no exact RUNNING result" in item for item in result.warnings))
        evidence = list((self.results_root / "_orphaned_locks").glob("*.lock.json"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0].read_text(encoding="utf-8")), owner)

    def test_normal_run_has_no_orphan_recovery_warning(self):
        result = self.runner().run("health")
        self.assertEqual(result.status, SUCCEEDED)
        self.assertFalse(any(
            "orphaned observation lock" in item for item in result.warnings
        ))

    def test_global_and_scheduled_controls_fail_closed(self):
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        manual = self.runner().run("health", trigger_source="manual")
        self.assertEqual(manual.status, FAILED)
        self.assertIn("globally disabled", manual.errors[0])

        state = self.store.load()
        state["controls"]["global_disabled"] = False
        state["controls"]["scheduled_runs_disabled"] = True
        self.store.save(state)
        scheduled = self.runner().run("health", trigger_source="scheduled")
        self.assertEqual(scheduled.status, FAILED)
        self.assertIn("Scheduled Curator runs are disabled", scheduled.errors[0])

        state["controls"]["scheduled_runs_disabled"] = False
        self.store.save(state)
        allowed = self.runner().run("health", trigger_source="scheduled")
        self.assertEqual(allowed.status, SUCCEEDED)

    def test_cli_observe_and_existing_audit_parser_remain_available(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "observe", "--job", "health", "--repository", str(self.root),
                "--results", str(self.results_root),
            ])
        self.assertEqual(code, 0)
        self.assertIn('"status": "SUCCEEDED"', output.getvalue())

        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            main(["observe", "--job", "unknown"])

        audit_result = Mock()
        audit_result.run_id = "AUD-COMPAT"
        audit_result.summary.return_value = {
            "findings": 0,
            "findings_by_classification": {},
        }
        audit_result.findings = []
        with patch("curator.__main__.CuratorAuditor") as auditor:
            auditor.return_value.audit.return_value = (audit_result, self.root / "run")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["audit", "--repository", str(self.root)])
        self.assertEqual(code, 0)
        auditor.return_value.audit.assert_called_once_with(
            unittest.mock.ANY
        )

    def application_state(self):
        excluded = self.results_root.resolve()
        values = {}
        for path in self.root.rglob("*"):
            if not path.is_file() or path == self.root / ".curator-observation.lock":
                continue
            try:
                path.resolve().relative_to(excluded)
                continue
            except ValueError:
                pass
            values[str(path.relative_to(self.root))] = path.read_bytes()
        return values

    def result(self, run_id, status, *, completed_at="2026-08-26T12:00:01+00:00", errors=()):
        return ObservationRunResult(
            run_id=run_id,
            job_type="health",
            execution_class=PURE_OBSERVATION,
            trigger_source="manual",
            scheduler_correlation_id="",
            repository_identity="repository",
            application_identity="gnojo-local",
            started_at=self.now.isoformat(),
            completed_at=completed_at,
            duration_seconds=None if status == RUNNING else 1.0,
            status=status,
            observation_counts=(),
            summary=(),
            warnings=(),
            errors=errors,
            policy_versions=(),
            lifecycle_versions=(),
            trusted_content_changed=False,
            curator_state_changed=False,
            operational_result_written=True,
        )


if __name__ == "__main__":
    unittest.main()
