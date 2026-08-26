import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_observation_dashboard_service import (
    CuratorObservationDashboardService,
)
from curator.observation_models import (
    FAILED,
    PURE_OBSERVATION,
    RUNNING,
    SKIPPED_OVERLAP,
    SUCCEEDED,
    ObservationRunResult,
)
from curator.observation_repository import ObservationResultRepository


class CuratorObservationDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = ObservationResultRepository(
            self.root / "curation_observations"
        )
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def result(
        self,
        run_id,
        job_type,
        status,
        *,
        started_at,
        counts=(),
        warnings=(),
        errors=(),
    ):
        completed_at = "" if status == RUNNING else started_at
        return ObservationRunResult(
            run_id=run_id,
            job_type=job_type,
            execution_class=PURE_OBSERVATION,
            trigger_source="manual",
            scheduler_correlation_id="",
            repository_identity="fixture",
            application_identity="gnojo-local",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=None if status == RUNNING else 1.25,
            status=status,
            observation_counts=tuple(counts),
            summary=(),
            warnings=tuple(warnings),
            errors=tuple(errors),
            policy_versions=(),
            lifecycle_versions=(),
            trusted_content_changed=False,
            curator_state_changed=False,
            operational_result_written=True,
        )

    def test_projection_covers_success_failure_running_overlap_and_empty_jobs(self):
        self.repository.create(self.result(
            "OBS-HEALTH-SUCCESS", "health", SUCCEEDED,
            started_at="2026-08-26T10:00:00+00:00",
            counts=(("required_directories", 3), ("missing_directories", 0)),
        ))
        self.repository.create(self.result(
            "OBS-HEALTH-FAILED", "health", FAILED,
            started_at="2026-08-26T11:00:00+00:00",
            errors=("Observation failed.",),
        ))
        self.repository.create(self.result(
            "OBS-AUDIT-RUNNING", "audit", RUNNING,
            started_at="2026-08-26T12:00:00+00:00",
        ))
        self.repository.create(self.result(
            "OBS-INTEGRITY-OVERLAP", "integrity", SKIPPED_OVERLAP,
            started_at="2026-08-26T13:00:00+00:00",
            warnings=("Another observation owns the lock.",),
        ))

        projection = CuratorObservationDashboardService(self.root).project()
        jobs = {item["job_type"]: item for item in projection["jobs"]}

        self.assertTrue(projection["has_results"])
        self.assertEqual(projection["running_count"], 1)
        self.assertEqual(projection["overlap_count"], 1)
        self.assertEqual(jobs["health"]["status"], FAILED)
        self.assertEqual(
            jobs["health"]["last_successful_at"], "2026-08-26T10:00:00+00:00"
        )
        self.assertEqual(
            jobs["health"]["last_failed_at"], "2026-08-26T11:00:00+00:00"
        )
        self.assertEqual(jobs["health"]["error_count"], 1)
        self.assertEqual(jobs["audit"]["status"], RUNNING)
        self.assertEqual(jobs["integrity"]["status"], SKIPPED_OVERLAP)
        self.assertFalse(jobs["progress-policy"]["has_result"])
        self.assertFalse(jobs["analytics"]["has_result"])

        dashboard = {
            "has_audit": False,
            "tasks": [],
            "recent_audits": [],
            "observations": projection,
        }
        with patch("app.app.CuratorDashboardService") as dashboard_service, patch(
            "app.app.CuratorBatchService"
        ) as batch_service:
            dashboard_service.return_value.dashboard.return_value = dashboard
            batch_service.return_value.latest.return_value = {}
            html = self.client.get("/curator").get_data(as_text=True)
        self.assertIn("Failed", html)
        self.assertIn("Running", html)
        self.assertIn("Skipped Overlap", html)
        self.assertIn("1 error", html)
        self.assertIn("No observation recorded yet", html)

    def test_dashboard_renders_policy_analytics_and_compact_integrity_counts(self):
        self.repository.create(self.result(
            "OBS-POLICY", "progress-policy", SUCCEEDED,
            started_at="2026-08-26T14:00:00+00:00",
            counts=(("supported_tasks", 4), ("eligible", 1), ("ineligible", 3)),
        ))
        self.repository.create(self.result(
            "OBS-ANALYTICS", "analytics", SUCCEEDED,
            started_at="2026-08-26T14:01:00+00:00",
            counts=(
                ("production_sessions", 12), ("workflows", 9),
                ("quality_findings", 2), ("frequently_confusing_steps", 1),
            ),
        ))
        self.repository.create(self.result(
            "OBS-INTEGRITY", "integrity", SUCCEEDED,
            started_at="2026-08-26T14:02:00+00:00",
            counts=(
                ("broken_relationships", 2), ("duplicate_groups", 1),
                ("missing_review_metadata", 3), ("orphaned_articles", 99),
                ("workflow_lifecycle_projections", 8),
            ),
        ))
        dashboard = {
            "has_audit": False,
            "tasks": [],
            "recent_audits": [],
            "observations": CuratorObservationDashboardService(self.root).project(),
        }
        with patch("app.app.CuratorDashboardService") as dashboard_service, patch(
            "app.app.CuratorBatchService"
        ) as batch_service:
            dashboard_service.return_value.dashboard.return_value = dashboard
            batch_service.return_value.latest.return_value = {}
            html = self.client.get("/curator").get_data(as_text=True)

        self.assertIn("Recent Curator Observations", html)
        self.assertIn("Supported tasks", html)
        self.assertIn("Eligible", html)
        self.assertIn("Ineligible", html)
        self.assertIn("Production sessions", html)
        self.assertIn("Reported confusing steps", html)
        self.assertIn("Broken relationships", html)
        self.assertNotIn("Orphaned articles", html)
        self.assertIn("OBS-POLICY", html)
        self.assertIn("No observation recorded yet", html)

    def test_dashboard_get_reads_results_without_mutating_any_state(self):
        self.repository.create(self.result(
            "OBS-READ-ONLY", "health", SUCCEEDED,
            started_at="2026-08-26T15:00:00+00:00",
            counts=(("required_directories", 3),),
        ))
        before = self._files()
        service = CuratorDashboardService(self.root)
        with patch("app.app.CuratorDashboardService", return_value=service), patch(
            "app.app.CuratorBatchService"
        ) as batch_service:
            batch_service.return_value.latest.return_value = {}
            response = self.client.get("/curator")
        after = self._files()

        self.assertEqual(response.status_code, 200)
        self.assertIn("OBS-READ-ONLY", response.get_data(as_text=True))
        self.assertEqual(after, before)

    def _files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
