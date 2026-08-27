from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import render_template

from app.app import app
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_stage_b_dashboard_service import (
    CuratorStageBDashboardService,
)
from app.services.curator_stage_b_reconciliation_service import (
    CuratorStageBReconciliationService,
)
from curator.memory import CuratorMemoryStore
from curator.reconciliation import StageBJournalEvent, StageBJournalRepository
from curator.stage_b_scheduled_repository import StageBScheduledRunRepository


class CuratorStageBDashboardServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.state = self.memory.load()
        self.state["controls"]["global_disabled"] = False
        self.state["controls"]["scheduled_runs_disabled"] = False
        self.state["controls"]["stage_b_scheduled_runs_disabled"] = False
        self.memory.save(self.state)
        self.journal = StageBJournalRepository(self.root / "curation_memory")

    def tearDown(self):
        self.temporary.cleanup()

    def _key(self, marker: str) -> str:
        return hashlib.sha256(marker.encode("utf-8")).hexdigest()

    def _append(
        self,
        capability_id: str,
        status: str,
        *,
        marker: str,
        task_id: str,
        finding_id: str,
        at: str,
        previous: StageBJournalEvent | None = None,
        reason: str | None = None,
    ) -> StageBJournalEvent:
        event = StageBJournalEvent.build(
            previous=previous,
            event_id=f"SBE-{marker.upper()}",
            run_id=f"STB-{marker.upper()}",
            correlation_id=f"COR-{marker.upper()}",
            capability_id=capability_id,
            capability_version=1,
            task_id=task_id,
            finding_id=finding_id,
            idempotency_key=self._key(marker),
            precondition_fingerprint="a" * 64,
            before_task_fingerprint="b" * 64,
            after_task_fingerprint="c" * 64,
            declared_mutation_fields=(
                "current_verification", "last_verified_fingerprint", "history"
            ),
            at=at,
            status=status,
            reason=reason or f"{status.title()} fixture event.",
        )
        self.journal.append(event)
        return event

    def _committed(
        self, capability_id: str, marker: str, task_id: str, finding_id: str,
    ) -> None:
        prepared = self._append(
            capability_id, "PREPARED", marker=marker, task_id=task_id,
            finding_id=finding_id, at="2026-08-27T10:00:00+00:00",
        )
        self._append(
            capability_id, "COMMITTED", marker=marker, task_id=task_id,
            finding_id=finding_id, at="2026-08-27T10:01:00+00:00",
            previous=prepared,
        )

    def _project(self):
        return CuratorStageBDashboardService(self.root).project(
            controls=self.state["controls"]
        )

    def test_all_five_capabilities_and_empty_acceptance_are_projected(self):
        projection = self._project()

        self.assertEqual(projection["journal_status"], "HEALTHY")
        self.assertEqual(len(projection["capabilities"]), 5)
        self.assertEqual(
            [item["capability_id"] for item in projection["capabilities"]],
            [item[0] for item in CuratorStageBDashboardService.CAPABILITIES],
        )
        self.assertTrue(all(
            item["acceptance"] == "NO_COMMITTED_ACCEPTANCE"
            for item in projection["capabilities"]
        ))

    def test_real_acceptance_shape_for_capabilities_four_and_five(self):
        early = "cur-wr-early-convergence-verification-refresh"
        signal = "cur-wr-signal-retention-verification-refresh"
        self._committed(early, "EARLY", "GKT-EARLY", "CUR-EARLY")
        self._committed(signal, "SIGNAL", "GKT-SIGNAL", "CUR-SIGNAL")

        projection = self._project()
        capabilities = {
            item["capability_id"]: item for item in projection["capabilities"]
        }

        self.assertEqual(
            capabilities[early]["acceptance"], "COMMITTED_ACCEPTANCE"
        )
        self.assertEqual(
            capabilities[signal]["acceptance"], "COMMITTED_ACCEPTANCE"
        )
        self.assertEqual(capabilities[early]["latest"]["task_id"], "GKT-EARLY")
        self.assertEqual(capabilities[signal]["latest"]["finding_id"], "CUR-SIGNAL")
        self.assertEqual(projection["counts"]["committed"], 2)
        self.assertEqual(projection["journal_status"], "HEALTHY")

    def test_skipped_failed_and_incomplete_events_remain_visible(self):
        progress = "cur-wr-progress-verification-refresh"
        terminal = "cur-wr-terminal-evidence-verification-refresh"
        evidence = "cur-wr-terminal-evidence-current-evidence-sync"
        self._append(
            progress, "SKIPPED", marker="SKIP", task_id="GKT-SKIP",
            finding_id="CUR-SKIP", at="2026-08-27T11:00:00+00:00",
            reason="Exact reconciliation already committed.",
        )
        self._append(
            terminal, "FAILED", marker="FAIL", task_id="GKT-FAIL",
            finding_id="CUR-FAIL", at="2026-08-27T11:01:00+00:00",
            reason="CAS precondition failed.",
        )
        self._append(
            evidence, "PREPARED", marker="PENDING", task_id="GKT-PENDING",
            finding_id="CUR-PENDING", at="2026-08-27T11:02:00+00:00",
        )

        projection = self._project()
        capabilities = {
            item["capability_id"]: item for item in projection["capabilities"]
        }

        self.assertEqual(projection["journal_status"], "INCOMPLETE")
        self.assertEqual(projection["counts"], {
            "incomplete_prepared": 1, "committed": 0, "failed": 1,
            "skipped": 1,
        })
        self.assertEqual(capabilities[progress]["latest_skipped"]["task_id"], "GKT-SKIP")
        self.assertEqual(capabilities[terminal]["acceptance"], "FAILED_ONLY")
        self.assertEqual(capabilities[terminal]["latest_failed"]["reason"], "CAS precondition failed.")
        self.assertEqual(capabilities[evidence]["acceptance"], "INCOMPLETE")
        self.assertEqual(len(capabilities[evidence]["incomplete_prepared"]), 1)

    def test_corrupt_journal_is_blocked_without_repair(self):
        invalid = self.root / "curation_memory" / "stage_b_reconciliations" / "invalid"
        invalid.mkdir(parents=True)
        before = sorted(
            (path.relative_to(self.root), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )

        projection = self._project()

        after = sorted(
            (path.relative_to(self.root), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        self.assertEqual(projection["journal_status"], "CORRUPT_BLOCKED")
        self.assertIn("invalid identity", projection["journal_error"])
        self.assertTrue(all(
            item["acceptance"] == "INCOMPLETE"
            for item in projection["capabilities"]
        ))
        self.assertEqual(after, before)

    def test_controls_and_unscheduled_stage_b_are_explicit(self):
        projection = self._project()
        self.assertFalse(projection["controls"]["global_disabled"])
        self.assertFalse(projection["controls"]["scheduled_runs_disabled"])
        self.assertFalse(
            projection["controls"]["stage_b_scheduled_runs_disabled"]
        )
        self.assertFalse(
            projection["controls"]["stage_b_scheduling_configured"]
        )
        self.assertEqual(
            projection["controls"]["stage_b_scheduling_message"],
            "Stage B scheduled execution is not configured.",
        )

    def test_latest_scheduled_runner_result_is_projected_read_only(self):
        repository = StageBScheduledRunRepository(self.root / "curation_memory")
        value = {
            "schema_version": 1,
            "runner_id": "STBS-DASHBOARD",
            "correlation_id": "COR-DASHBOARD",
            "trigger_source": "scheduled",
            "started_at": "2026-08-28T20:15:00+00:00",
            "completed_at": "2026-08-28T20:15:02+00:00",
            "status": "SUCCEEDED",
            "allowlisted_capabilities": [
                {"id": "cur-wr-early-convergence-verification-refresh", "version": 1},
                {"id": "cur-wr-signal-retention-verification-refresh", "version": 1},
            ],
            "discovered_count": 2,
            "preflight_skipped_count": 0,
            "committed_no_op_count": 0,
            "committed_count": 2,
            "runtime_skipped_count": 0,
            "failed_count": 0,
            "per_capability_counts": {},
            "last_processed_task": "GKT-SIGNAL",
            "failure_reason": "",
        }
        repository.create_running(dict(value, status="RUNNING", completed_at=""))
        repository.finalize(value["runner_id"], value)
        before = repository.get(value["runner_id"])

        projection = self._project()

        self.assertEqual(projection["scheduled_run"], value)
        self.assertEqual(projection["scheduled_run_error"], "")
        self.assertEqual(repository.get(value["runner_id"]), before)

    def test_dashboard_projection_is_read_only_and_preserves_stage_a_projection(self):
        before = sorted(
            (path.relative_to(self.root), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        stage_a = {"has_results": True, "running_count": 0, "overlap_count": 0,
                   "jobs": [{"job_type": "health", "has_result": True}]}

        with patch(
            "app.services.curator_dashboard_service."
            "CuratorObservationDashboardService.project",
            return_value=stage_a,
        ), patch.object(
            CuratorStageBReconciliationService,
            "run",
            side_effect=AssertionError("Dashboard must not execute Stage B."),
        ):
            dashboard = CuratorDashboardService(self.root).dashboard()

        after = sorted(
            (path.relative_to(self.root), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        self.assertEqual(dashboard["observations"], stage_a)
        self.assertEqual(len(dashboard["stage_b"]["capabilities"]), 5)
        self.assertEqual(after, before)


class CuratorStageBDashboardTemplateTests(unittest.TestCase):
    def test_dashboard_renders_compact_stage_b_operational_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = StageBJournalRepository(root / "curation_memory")
            key = hashlib.sha256(b"accepted").hexdigest()
            prepared = StageBJournalEvent.build(
                event_id="SBE-PREPARED", run_id="STB-ACCEPT", correlation_id="COR-ACCEPT",
                capability_id="cur-wr-early-convergence-verification-refresh",
                capability_version=1, task_id="GKT-ACCEPT", finding_id="CUR-ACCEPT",
                idempotency_key=key, precondition_fingerprint="a" * 64,
                before_task_fingerprint="b" * 64, after_task_fingerprint="c" * 64,
                declared_mutation_fields=("current_verification", "last_verified_fingerprint", "history"),
                at="2026-08-27T12:00:00+00:00", status="PREPARED",
                reason="Prepared fixture event.",
            )
            journal.append(prepared)
            journal.append(StageBJournalEvent.build(
                previous=prepared, event_id="SBE-COMMITTED", run_id="STB-ACCEPT",
                correlation_id="COR-ACCEPT",
                capability_id="cur-wr-early-convergence-verification-refresh",
                capability_version=1, task_id="GKT-ACCEPT", finding_id="CUR-ACCEPT",
                idempotency_key=key, precondition_fingerprint="a" * 64,
                before_task_fingerprint="b" * 64, after_task_fingerprint="c" * 64,
                declared_mutation_fields=("current_verification", "last_verified_fingerprint", "history"),
                at="2026-08-27T12:01:00+00:00", status="COMMITTED",
                reason="Verification state refreshed without lifecycle changes.",
            ))
            run_repository = StageBScheduledRunRepository(root / "curation_memory")
            scheduled = {
                "schema_version": 1, "runner_id": "STBS-TEMPLATE",
                "correlation_id": "COR-TEMPLATE", "trigger_source": "scheduled",
                "started_at": "2026-08-27T20:15:00+00:00",
                "completed_at": "2026-08-27T20:15:01+00:00",
                "status": "SUCCEEDED", "allowlisted_capabilities": [
                    {"id": "cur-wr-early-convergence-verification-refresh", "version": 1},
                    {"id": "cur-wr-signal-retention-verification-refresh", "version": 1},
                ],
                "discovered_count": 1, "preflight_skipped_count": 0,
                "committed_no_op_count": 0, "committed_count": 1,
                "runtime_skipped_count": 0, "failed_count": 0,
                "per_capability_counts": {}, "last_processed_task": "GKT-ACCEPT",
                "failure_reason": "",
            }
            run_repository.create_running(
                dict(scheduled, status="RUNNING", completed_at="")
            )
            run_repository.finalize(scheduled["runner_id"], scheduled)
            projection = CuratorStageBDashboardService(root).project(
                controls={"global_disabled": False, "scheduled_runs_disabled": False,
                          "stage_b_scheduled_runs_disabled": False}
            )

            with app.test_request_context():
                html = render_template(
                    "curator_dashboard.html",
                    dashboard={"has_audit": False, "observations": {"jobs": []},
                               "stage_b": projection},
                    status_kind="info", status_message="", assisted_batch={},
                )

        self.assertIn("Stage B Reconciliation", html)
        for capability_id, _, _ in CuratorStageBDashboardService.CAPABILITIES:
            self.assertIn(capability_id, html)
        self.assertIn("Committed Acceptance", html)
        self.assertIn("GKT-ACCEPT", html)
        self.assertIn("CUR-ACCEPT", html)
        self.assertIn("STB-ACCEPT", html)
        self.assertIn("COR-ACCEPT", html)
        self.assertIn("Stage B scheduled execution is not configured.", html)
        self.assertIn("Stage B scheduled authority", html)
        self.assertIn("Latest scheduled Stage B run", html)
        self.assertIn("STBS-TEMPLATE", html)
        self.assertIn("COR-TEMPLATE", html)
        self.assertIn("GKT-ACCEPT", html)
        self.assertIn("Recent Curator Observations", html)


if __name__ == "__main__":
    unittest.main()
