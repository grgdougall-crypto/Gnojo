from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curator.governance import CuratorGovernancePolicy
from curator.memory import CuratorMemoryStore
from curator.models import AuditFilter, Finding
from curator.tasks import KnowledgeTaskService


class CuratorContentQualityBridgeError(ValueError):
    pass


class _ConfusingStepTaskService(KnowledgeTaskService):
    @staticmethod
    def durable_identity(finding: Finding) -> str:
        return finding.identifier


class CuratorContentQualityBridgeService:
    """Send one eligible confusing-step aggregate to human-governed Curator review."""

    QUALITY_RULE = "CQ-FREQUENTLY-CONFUSING-STEP"
    FINDING_TYPE = "frequently_confusing_step"

    def __init__(self, repository_root: Path | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.repository_root / "curation_memory")

    def send(self, item: dict[str, Any]) -> dict[str, Any]:
        self._validate(item)
        observed_at = str(item.get("measured_at") or datetime.now(timezone.utc).isoformat())
        finding = self._finding(item)
        state = self.store.load()
        CuratorGovernancePolicy.authorize(
            "targeted_audit", "create_knowledge_tasks", state.get("controls")
        )
        reconciliation = _ConfusingStepTaskService().reconcile(
            state,
            [finding],
            [],
            run_id=f"content-quality:{observed_at}",
            observed_at=observed_at,
            filters=AuditFilter(content_type="workflow_node"),
        )
        task_id = reconciliation["observed"][0]
        task = state["tasks"][task_id]
        task["quality_baseline"] = dict(finding.provenance["quality_baseline"])
        self.store.save(state)
        return task

    def tracked_task_id(self, item: dict[str, Any]) -> str | None:
        if not self._eligible(item):
            return None
        finding = self._finding(item)
        identity = _ConfusingStepTaskService.durable_identity(finding)
        expected = KnowledgeTaskService.task_id(identity)
        tasks = self.store.load().get("tasks", {})
        if expected in tasks:
            return expected
        return next(
            (task_id for task_id, task in tasks.items() if task.get("durable_identity") == identity),
            None,
        )

    def mark_tracked(self, report: dict[str, Any]) -> None:
        for item in report.get("action_queue", []):
            task_id = self.tracked_task_id(item)
            if task_id:
                item["curator_task_id"] = task_id

    def _finding(self, item: dict[str, Any]) -> Finding:
        workflow_id = str(item["workflow_id"])
        node_id = str(item["node_id"])
        report_count = int(item["report_count"])
        sample_count = int(item["sample_count"])
        clarity = item.get("aggregate_clarity")
        baseline = {
            "workflow_id": workflow_id,
            "workflow_version": item.get("workflow_version"),
            "node_id": node_id,
            "quality_rule": self.QUALITY_RULE,
            "report_count": report_count,
            "sample_count": sample_count,
            "aggregate_clarity": clarity,
            "measured_at": str(item["measured_at"]),
        }
        evidence = [
            f"{report_count} of {sample_count} feedback reports identified this step as confusing.",
            f"Aggregate workflow clarity: {clarity}/5." if clarity is not None else "Aggregate workflow clarity is not available.",
        ]
        identity = f"{self.QUALITY_RULE}|{workflow_id}|{node_id}"
        return Finding(
            identifier=identity,
            classification="recommendation",
            finding_type=self.FINDING_TYPE,
            severity=str(item.get("priority") or "medium"),
            confidence="high" if report_count >= 3 else "medium",
            content_type="workflow_node",
            content_identifier=f"{workflow_id}:{node_id}",
            title="Frequently confusing step",
            explanation="Runtime feedback repeatedly identifies this workflow step as confusing. Human review is required before any workflow change.",
            evidence=evidence,
            rule=self.QUALITY_RULE,
            recommended_action="Review the affected workflow step and decide whether a governed improvement should be prepared.",
            domain="workflow",
            future_automated_fix=False,
            provenance={"source": "content_quality_runtime_feedback", "quality_baseline": baseline},
        )

    def _validate(self, item: dict[str, Any]) -> None:
        if not self._eligible(item):
            raise CuratorContentQualityBridgeError(
                "Only an eligible Frequently confusing step finding can be sent to Curator."
            )
        for field in ("workflow_id", "node_id", "report_count", "sample_count", "measured_at"):
            if item.get(field) in (None, ""):
                raise CuratorContentQualityBridgeError(f"Content Quality finding is missing {field}.")

    def _eligible(self, item: dict[str, Any]) -> bool:
        return (
            item.get("kind") == "confusing_step"
            and item.get("quality_rule") == self.QUALITY_RULE
            and bool(item.get("workflow_id"))
            and bool(item.get("node_id"))
        )
