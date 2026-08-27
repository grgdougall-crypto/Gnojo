from __future__ import annotations

from pathlib import Path
from typing import Any

from curator.observation_models import FAILED, RUNNING, SKIPPED_OVERLAP, SUCCEEDED
from curator.observation_repository import ObservationResultRepository


class CuratorObservationDashboardService:
    """Project persisted Stage A observations into compact dashboard data."""

    JOBS = (
        ("health", "Health"),
        ("audit", "Curator Audit"),
        ("integrity", "Knowledge Integrity"),
        ("progress-policy", "Progress Policy"),
        ("analytics", "Troubleshooting Analytics"),
    )
    COUNT_LABELS = {
        "health": (
            ("required_directories", "Repository areas"),
            ("missing_directories", "Missing areas"),
            ("curator_tasks", "Curator tasks"),
        ),
        "audit": (
            ("inventory_records", "Inventory records"),
            ("findings", "Findings"),
            ("coverage_gaps", "Coverage gaps"),
        ),
        "integrity": (
            ("broken_relationships", "Broken relationships"),
            ("duplicate_groups", "Duplicate groups"),
            ("missing_review_metadata", "Missing review metadata"),
            ("workflow_lifecycle_projections", "Workflow projections"),
        ),
        "progress-policy": (
            ("supported_tasks", "Supported tasks"),
            ("eligible", "Eligible"),
            ("ineligible", "Ineligible"),
        ),
        "analytics": (
            ("production_sessions", "Production sessions"),
            ("workflows", "Workflows"),
            ("quality_findings", "Quality findings"),
            ("frequently_confusing_steps", "Reported confusing steps"),
        ),
    }

    def __init__(self, repository_root: Path):
        self.repository = ObservationResultRepository(
            Path(repository_root).resolve() / "curation_observations"
        )

    def project(self) -> dict[str, Any]:
        results = sorted(
            self.repository.list_recent(limit=100),
            key=lambda result: (result.started_at, result.run_id),
            reverse=True,
        )
        jobs = [self._project_job(job_type, label, results) for job_type, label in self.JOBS]
        return {
            "has_results": any(job["has_result"] for job in jobs),
            "running_count": sum(job["status"] == RUNNING for job in jobs),
            "overlap_count": sum(job["status"] == SKIPPED_OVERLAP for job in jobs),
            "jobs": jobs,
        }

    def _project_job(self, job_type: str, label: str, results: list) -> dict[str, Any]:
        matching = [result for result in results if result.job_type == job_type]
        latest = matching[0] if matching else None
        successful = next((item for item in matching if item.status == SUCCEEDED), None)
        failed = next((item for item in matching if item.status == FAILED), None)
        counts = dict(latest.observation_counts) if latest else {}
        return {
            "job_type": job_type,
            "label": label,
            "has_result": latest is not None,
            "status": latest.status if latest else "",
            "trigger_source": self._trigger_label(
                latest.trigger_source if latest else ""
            ),
            "started_at": latest.started_at if latest else "",
            "completed_at": latest.completed_at if latest else "",
            "last_successful_at": successful.completed_at if successful else "",
            "last_failed_at": failed.completed_at if failed else "",
            "duration_seconds": latest.duration_seconds if latest else None,
            "warning_count": len(latest.warnings) if latest else 0,
            "error_count": len(latest.errors) if latest else 0,
            "run_id": latest.run_id if latest else "",
            "counts": [
                {"key": key, "label": count_label, "value": counts[key]}
                for key, count_label in self.COUNT_LABELS[job_type]
                if key in counts
            ],
        }

    @staticmethod
    def _trigger_label(value: str) -> str:
        return {"manual": "Manual", "scheduled": "Scheduled"}.get(
            str(value or "").casefold(), "Unknown"
        )
