from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)
from curator.growth import CuratorGrowthService
from curator.memory import CuratorMemoryStore
from curator.resolution import ResolutionPackageError, ResolutionPackageRepository


class CuratorConfusingStepImprovementError(ResolutionPackageError):
    pass


class CuratorConfusingStepImprovementService:
    """Govern one human-authored help-text proposal for a confusing-step task."""

    PROPOSAL_TYPE = "confusing_step_help_text"
    FINDING_TYPE = "frequently_confusing_step"
    QUALITY_RULE = "CQ-FREQUENTLY-CONFUSING-STEP"
    MINIMUM_AFTER_SAMPLES = 2

    def __init__(self, repository_root: Path | None = None, history_path: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.packages = ResolutionPackageRepository(self.root / "curation_memory")
        self.drafts = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        self.publications = WorkflowPublicationService(
            self.root / "app" / "workflow_publications"
        )
        self.history = TroubleshootingHistoryService(
            history_path or self.root / "app" / "troubleshooting_history"
        )

    def get(self, task_id: str) -> dict[str, Any] | None:
        package = self.packages.get(task_id)
        return package if package and package.get("proposal_type") == self.PROPOSAL_TYPE else None

    def prepare(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        existing = self.get(task_id)
        if existing:
            return existing
        workflow_id, node_id, filename, node = self._draft_node(task)
        baseline = deepcopy(task.get("quality_baseline") or {})
        before_version = self._authoritative_baseline_version(
            task, workflow_id, node_id, baseline
        )
        baseline["workflow_version"] = before_version
        current_help_text = str(node.get("help_text") or "")
        return self.packages.save({
            "proposal_type": self.PROPOSAL_TYPE,
            "task_id": task_id,
            "finding_id": task.get("finding_id"),
            "workflow_id": workflow_id,
            "workflow_filename": filename,
            "node_id": node_id,
            "before_workflow_version": before_version,
            "current_help_text": current_help_text,
            "proposed_help_text": current_help_text,
            "quality_baseline": baseline,
            "status": "proposed",
            "approved_by": None,
            "approved_at": None,
            "approval_note": "",
            "approved_proposal_version": None,
            "published_version": None,
            "published_at": None,
            "workflow_changed_event_id": None,
            "measurement": None,
        })

    def edit(self, task_id: str, proposed_help_text: str) -> dict[str, Any]:
        package = self._package(task_id)
        if package.get("status") != "proposed":
            raise CuratorConfusingStepImprovementError(
                "Only an unapproved proposal can be edited."
            )
        text = str(proposed_help_text or "").strip()
        if not text:
            raise CuratorConfusingStepImprovementError("Proposed help text is required.")
        if len(text) > 1000:
            raise CuratorConfusingStepImprovementError(
                "Proposed help text must be 1000 characters or fewer."
            )
        if package.get("proposed_help_text") == text:
            return package
        package["proposed_help_text"] = text
        return self.packages.save(package)

    def approve(self, task_id: str, *, reviewer: str, note: str) -> dict[str, Any]:
        package = self._package(task_id)
        if package.get("status") != "proposed":
            raise CuratorConfusingStepImprovementError(
                "Only a proposed improvement can be approved."
            )
        reviewer, note = str(reviewer or "").strip(), str(note or "").strip()
        if not reviewer or not note:
            raise CuratorConfusingStepImprovementError(
                "Reviewer identity and an approval note are required."
            )
        normalized = reviewer.casefold().replace("_", " ").replace("-", " ")
        if normalized in {"curator", "system", "agent", "automation", "gnojo curator"}:
            raise CuratorConfusingStepImprovementError(
                "An automated identity cannot approve an editorial proposal."
            )
        if not str(package.get("proposed_help_text") or "").strip():
            raise CuratorConfusingStepImprovementError("Proposed help text is required.")
        now = self._now()
        package.update({
            "status": "human_approved",
            "approved_by": reviewer,
            "approved_at": now,
            "approval_note": note,
            "approved_proposal_version": int(package.get("version") or 0) + 1,
        })
        return self.packages.save(package)

    def handoff(self, task_id: str) -> dict[str, str]:
        package = self._package(task_id)
        if package.get("status") not in {"human_approved", "published", "measured"}:
            raise CuratorConfusingStepImprovementError(
                "Human approval is required before opening the workflow handoff."
            )
        self._published_or_draft_node(package["workflow_id"], package["node_id"], draft=True)
        return {
            "filename": package["workflow_filename"],
            "node_id": package["node_id"],
        }

    def record_published_version(self, task_id: str) -> dict[str, Any]:
        package = self._package(task_id)
        if package.get("status") not in {"human_approved", "published", "measured"}:
            raise CuratorConfusingStepImprovementError(
                "Human approval is required before recording a publication."
            )
        if package.get("published_version"):
            return package
        workflow_id, node_id = package["workflow_id"], package["node_id"]
        status = self.publications.status(workflow_id)
        after_version = status.get("current_version")
        before_version = package.get("before_workflow_version")
        if not isinstance(after_version, int) or after_version <= before_version:
            raise CuratorConfusingStepImprovementError(
                "Publish a new workflow version after the baseline before recording it."
            )
        snapshot = self.publications.load_version(workflow_id, after_version)
        node = (snapshot.get("workflow", {}).get("nodes") or {}).get(node_id)
        if not isinstance(node, dict):
            raise CuratorConfusingStepImprovementError(
                "The affected node does not exist in the published workflow version."
            )
        if str(node.get("help_text") or "").strip() != str(package["proposed_help_text"]).strip():
            raise CuratorConfusingStepImprovementError(
                "The published node does not contain the approved help text."
            )
        event = CuratorGrowthService(self.memory).enqueue_event(
            "workflow_changed",
            f"{workflow_id}:{node_id}",
            actor=package["approved_by"],
            metadata={
                "task_id": task_id,
                "workflow_id": workflow_id,
                "node_id": node_id,
                "proposal_type": self.PROPOSAL_TYPE,
                "proposal_version": package.get("approved_proposal_version"),
                "before_version": before_version,
                "after_version": after_version,
            },
        )
        publication = snapshot.get("publication") or {}
        package.update({
            "status": "published",
            "published_version": after_version,
            "published_at": publication.get("published_at"),
            "workflow_changed_event_id": event["event_id"],
        })
        return self.packages.save(package)

    def measure(self, task_id: str) -> dict[str, Any]:
        package = self._package(task_id)
        if package.get("status") not in {"published", "measured"}:
            raise CuratorConfusingStepImprovementError(
                "Record the published workflow version before measuring the outcome."
            )
        records = self.history.list(500, environment="production")
        before = self._cohort(
            records, package["workflow_id"], package["node_id"],
            package["before_workflow_version"],
        )
        after = self._cohort(
            records, package["workflow_id"], package["node_id"],
            package["published_version"],
        )
        state = (
            "insufficient_post_change_evidence"
            if after["sample_count"] < self.MINIMUM_AFTER_SAMPLES
            else "observational_evidence_available"
        )
        rate_change = round(after["confusing_rate"] - before["confusing_rate"], 1)
        relative_change = (
            round((rate_change / before["confusing_rate"]) * 100, 1)
            if before["confusing_rate"] else None
        )
        clarity_change = (
            round(after["aggregate_clarity"] - before["aggregate_clarity"], 1)
            if after["aggregate_clarity"] is not None
            and before["aggregate_clarity"] is not None else None
        )
        evidence = {
            "state": state,
            "label": (
                "Insufficient post-change evidence"
                if state == "insufficient_post_change_evidence"
                else "Observational evidence; not proof of causation"
            ),
            "before": before,
            "after": after,
            "confusing_rate_change_points": rate_change,
            "confusing_rate_relative_change_percent": relative_change,
            "aggregate_clarity_change": clarity_change,
        }
        previous = package.get("measurement") or {}
        comparable_previous = {key: value for key, value in previous.items() if key != "measured_at"}
        if comparable_previous == evidence:
            return package
        package["measurement"] = {**evidence, "measured_at": self._now()}
        package["status"] = "measured"
        return self.packages.save(package)

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.memory.load().get("tasks", {}).get(task_id)
        if not task:
            raise CuratorConfusingStepImprovementError("Knowledge Task was not found.")
        if (task.get("finding_type") != self.FINDING_TYPE
                or task.get("curator_rule") != self.QUALITY_RULE):
            raise CuratorConfusingStepImprovementError(
                "Only a Frequently confusing step task is eligible."
            )
        return task

    def _package(self, task_id: str) -> dict[str, Any]:
        self._task(task_id)
        package = self.get(task_id)
        if not package:
            raise CuratorConfusingStepImprovementError("Prepare the help-text proposal first.")
        return package

    def _draft_node(self, task: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
        workflow_id, separator, node_id = str(task.get("content_identifier") or "").partition(":")
        if not separator or not workflow_id or not node_id:
            raise CuratorConfusingStepImprovementError("The affected workflow node is invalid.")
        draft = next(
            (item for item in self.drafts.list_drafts()
             if item.get("workflow_id") == workflow_id and not item.get("is_damaged")),
            None,
        )
        if not draft:
            raise CuratorConfusingStepImprovementError("The affected workflow draft was not found.")
        node = self._published_or_draft_node(workflow_id, node_id, draft=True)[1]
        return workflow_id, node_id, draft["filename"], node

    def _authoritative_baseline_version(
        self, task: dict[str, Any], workflow_id: str, node_id: str,
        baseline: dict[str, Any],
    ) -> int:
        version = baseline.get("workflow_version")
        if isinstance(version, int) and version >= 1:
            return version
        measured_at = str(baseline.get("measured_at") or task.get("first_seen") or "")
        candidates = []
        for record in self.history.list(500, environment="production"):
            feedback = record.get("feedback")
            submitted_at = str((feedback or {}).get("submitted_at") or "")
            record_version = record.get("workflow_version")
            if (record.get("workflow_id") == workflow_id
                    and isinstance(feedback, dict)
                    and feedback.get("confusing_step") == node_id
                    and isinstance(record_version, int) and record_version >= 1
                    and (not measured_at or not submitted_at or submitted_at <= measured_at)):
                candidates.append((submitted_at, record_version))
        if candidates:
            return sorted(candidates)[-1][1]
        current = self.publications.status(workflow_id).get("current_version")
        if isinstance(current, int) and current >= 1:
            return current
        raise CuratorConfusingStepImprovementError(
            "An authoritative published baseline version is required before preparing this proposal."
        )

    def _published_or_draft_node(
        self, workflow_id: str, node_id: str, *, draft: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if draft:
            item = next(
                (value for value in self.drafts.list_drafts()
                 if value.get("workflow_id") == workflow_id and not value.get("is_damaged")),
                None,
            )
            workflow = self.drafts.get_draft(item["filename"]) if item else None
        else:
            snapshot = self.publications.load_current(workflow_id)
            workflow = snapshot.get("workflow") if snapshot else None
        node = (workflow or {}).get("nodes", {}).get(node_id)
        if not isinstance(workflow, dict) or not isinstance(node, dict):
            raise CuratorConfusingStepImprovementError("The affected workflow node was not found.")
        return workflow, node

    @staticmethod
    def _cohort(
        records: list[dict[str, Any]], workflow_id: str, node_id: str, version: int
    ) -> dict[str, Any]:
        feedback = [
            record["feedback"] for record in records
            if record.get("workflow_id") == workflow_id
            and record.get("workflow_version") == version
            and node_id in (record.get("path") or [])
            and isinstance(record.get("feedback"), dict)
        ]
        confusing_count = sum(item.get("confusing_step") == node_id for item in feedback)
        clarity = [item["clarity"] for item in feedback if isinstance(item.get("clarity"), int)]
        sample_count = len(feedback)
        return {
            "workflow_version": version,
            "sample_count": sample_count,
            "confusing_step_count": confusing_count,
            "confusing_rate": round((confusing_count / sample_count) * 100, 1) if sample_count else 0.0,
            "aggregate_clarity": round(sum(clarity) / len(clarity), 1) if clarity else None,
        }

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
