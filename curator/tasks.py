from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .models import AuditFilter, Finding, InventoryRecord


ACTIVE_STATUSES = {"open", "in_progress"}


class KnowledgeTaskService:
    """Reconcile stable findings into persistent, non-duplicating tasks."""

    def reconcile(
        self,
        state: dict[str, Any],
        findings: list[Finding],
        inventory: list[InventoryRecord],
        *,
        run_id: str,
        observed_at: str,
        filters: AuditFilter,
    ) -> dict[str, Any]:
        tasks = state.setdefault("tasks", {})
        records = {item.identifier: item for item in inventory}
        observed_task_ids: set[str] = set()
        created: list[str] = []
        returned: list[str] = []

        for finding in findings:
            durable_identity = self.durable_identity(finding)
            task_id = self._existing_task_id(tasks, finding, durable_identity) or self.task_id(durable_identity)
            observed_task_ids.add(task_id)
            task = tasks.get(task_id)
            record = self._record_for(finding, records)
            if task is None:
                task = self._new_task(task_id, finding, record, run_id, observed_at)
                tasks[task_id] = task
                created.append(task_id)
            else:
                task["durable_identity"] = durable_identity
                task.setdefault("execution_mode", self.execution_mode(task))
                task["finding_id"] = finding.identifier
                if task["status"] == "resolved":
                    task["status"] = "open"
                    task["times_returned"] = int(task.get("times_returned", 0)) + 1
                    task.setdefault("resolution_history", []).append({
                        "at": observed_at,
                        "actor": "Curator",
                        "event": "returned",
                        "run_id": run_id,
                        "note": "The same deterministic finding was observed again.",
                    })
                    returned.append(task_id)
                task["last_seen"] = observed_at
                task["times_observed"] = int(task.get("times_observed", 0)) + 1
                task["trend"] = "returned" if task_id in returned else "recurring"
                task["confidence"] = self._confidence(task.get("confidence", "low"), finding.confidence)
                task["current_evidence"] = list(finding.evidence)
                task["recommended_action"] = finding.recommended_action
                task["explanation"] = finding.explanation
                task["curator_rule"] = finding.rule
                task["future_automated_fix"] = finding.future_automated_fix
                task["safety_level"] = finding.safety_level
                task["provenance"] = dict(finding.provenance)
                task["knowledge_debt_score"] = 0.0
            task.setdefault("history", []).append({
                "event": "observed",
                "run_id": run_id,
                "at": observed_at,
                "confidence": finding.confidence,
                "evidence": list(finding.evidence),
            })

        resolved: list[str] = []
        if self._is_full_audit(filters):
            for task_id, task in tasks.items():
                if task_id in observed_task_ids or task.get("status") not in ACTIVE_STATUSES:
                    continue
                task["status"] = "resolved"
                task["trend"] = "resolved"
                task["resolved_run_id"] = run_id
                task.setdefault("resolution_history", []).append({
                    "at": observed_at,
                    "actor": "Curator",
                    "event": "resolved",
                    "run_id": run_id,
                    "note": "The finding was not observed during a complete audit.",
                })
                resolved.append(task_id)

        return {
            "created": created,
            "returned": returned,
            "resolved": resolved,
            "observed": sorted(observed_task_ids),
            "tasks": tasks,
        }

    @staticmethod
    def task_id(finding_id: str) -> str:
        digest = hashlib.sha256(finding_id.encode("utf-8")).hexdigest()[:12].upper()
        return f"GKT-{digest}"

    @staticmethod
    def durable_identity(finding: Finding) -> str:
        """Identify the editorial work item, independent of changing evidence text."""
        parts = [finding.rule, finding.content_type, finding.content_identifier, finding.finding_type]
        if finding.content_type in {"workflow", "workflow_node"} and finding.provenance:
            parts.extend((str(finding.provenance.get("lifecycle") or ""),
                          str(finding.provenance.get("source_path") or "")))
        return "|".join(parts)

    @staticmethod
    def _existing_task_id(tasks: dict[str, dict[str, Any]], finding: Finding,
                          durable_identity: str) -> str | None:
        for task_id, task in tasks.items():
            if task.get("durable_identity") == durable_identity:
                return task_id
            # Migration path for tasks created before durable identities existed.
            if task.get("finding_id") == finding.identifier:
                return task_id
            same_legacy_fields = (task.get("curator_rule") == finding.rule
                    and task.get("content_type") == finding.content_type
                    and task.get("content_identifier") == finding.content_identifier
                    and task.get("finding_type") == finding.finding_type)
            task_provenance = task.get("provenance") or {}
            finding_provenance = finding.provenance or {}
            same_provenance = (not task_provenance or not finding_provenance
                               or (task_provenance.get("lifecycle") == finding_provenance.get("lifecycle")
                                   and task_provenance.get("source_path") == finding_provenance.get("source_path")))
            if same_legacy_fields and same_provenance:
                return task_id
        return None

    def _new_task(
        self,
        task_id: str,
        finding: Finding,
        record: InventoryRecord | None,
        run_id: str,
        observed_at: str,
    ) -> dict[str, Any]:
        workflow_id = finding.content_identifier.split(":", 1)[0] if finding.content_type in {"workflow", "workflow_node"} else ""
        return {
            "task_id": task_id,
            "finding_id": finding.identifier,
            "durable_identity": self.durable_identity(finding),
            "status": "open",
            "owner": self._owner(finding),
            "priority": finding.severity.title(),
            "classification": finding.classification.title(),
            "finding_type": finding.finding_type,
            "title": finding.title,
            "domain": finding.domain,
            "knowledge_debt_score": 0.0,
            "date_created": observed_at,
            "first_seen": observed_at,
            "last_seen": observed_at,
            "created_run_id": run_id,
            "times_observed": 1,
            "times_returned": 0,
            "trend": "new",
            "related_content": [finding.content_identifier],
            "related_workflows": [workflow_id] if workflow_id else [],
            "related_articles": [finding.content_identifier] if finding.content_type == "article" else [],
            "related_commands": [finding.content_identifier] if finding.content_type == "command" else [],
            "related_scripts": [finding.content_identifier] if finding.content_type == "script" else [],
            "content_type": finding.content_type,
            "content_identifier": finding.content_identifier,
            "category": record.category if record else "",
            "platform": record.platform if record else "",
            "recommended_action": finding.recommended_action,
            "explanation": finding.explanation,
            "curator_rule": finding.rule,
            "future_automated_fix": finding.future_automated_fix,
            "safety_level": finding.safety_level,
            "provenance": dict(finding.provenance),
            "confidence": finding.confidence,
            "evidence": list(finding.evidence),
            "current_evidence": list(finding.evidence),
            "execution_mode": self.execution_mode({
                "classification": finding.classification,
                "finding_type": finding.finding_type,
                "domain": finding.domain,
                "future_automated_fix": finding.future_automated_fix,
            }),
            "history": [],
            "resolution_history": [],
        }

    @staticmethod
    def execution_mode(task: dict[str, Any]) -> str:
        """Classify authority conservatively; adapter approval is evaluated separately."""
        classification = str(task.get("classification") or "").casefold()
        finding_type = str(task.get("finding_type") or "").casefold()
        domain = str(task.get("domain") or "").casefold()
        human_markers = ("taxonomy", "security", "publishing", "safety", "scope")
        if (classification == "recommendation" or domain == "taxonomy"
                or any(marker in finding_type for marker in human_markers)):
            return "HUMAN_DECISION"
        if classification in {"risk", "opportunity"}:
            return "ASSISTED"
        # A defect is only autonomous when a separate enabled trusted adapter is
        # attached by the repair planner. Findings alone cannot grant authority.
        return "ASSISTED"

    @staticmethod
    def _owner(finding: Finding) -> str:
        if finding.classification == "recommendation":
            return "Curator"
        if finding.domain == "source":
            return "Researcher"
        if finding.content_type == "script":
            return "Script Engineer"
        if finding.domain == "workflow":
            return "Workflow Designer"
        if finding.domain in {"application", "test"} or finding.classification == "defect":
            return "QA Reviewer"
        if finding.finding_type == "missing_safety_guidance":
            return "Human"
        return "Curator"

    @staticmethod
    def _record_for(finding: Finding, records: dict[str, InventoryRecord]) -> InventoryRecord | None:
        return records.get(finding.content_identifier) or records.get(finding.content_identifier.split(":", 1)[0])

    @staticmethod
    def _confidence(previous: str, current: str) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        return previous if order.get(previous, 0) >= order.get(current, 0) else current

    @staticmethod
    def _is_full_audit(filters: AuditFilter) -> bool:
        return not any((filters.platform, filters.category, filters.content_type, filters.severity, filters.changed_since))
