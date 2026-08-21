from __future__ import annotations

from copy import deepcopy
from typing import Any

from curator.workflow_reasoning import WorkflowReasoningAuditor
from curator.calibration import ReasoningCalibrationService

from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService


class CuratorTaskInventoryService:
    """Filter persistent tasks for review without changing task order or state."""

    DISPOSITIONS = {
        "NOT_REVIEWED": "Not Reviewed",
        "USEFUL": "Useful",
        "INTENTIONAL": "Intentional",
        "FALSE_POSITIVE": "False Positive",
    }

    def __init__(self, repository_root):
        self.lifecycle = CuratorWorkflowLifecycleService(repository_root)

    def filter(self, tasks: list[dict[str, Any]], filters: dict[str, str]) -> dict[str, Any]:
        enriched = [self._enrich(task) for task in tasks]
        options = self._options(enriched)
        selected = {
            "status": filters.get("status", "").strip(),
            "include_resolved": filters.get("include_resolved", "").strip(),
            "classification": filters.get("classification", "").strip(),
            "workflow": filters.get("workflow", "").strip(),
            "family": filters.get("family", "").strip(),
            "rule": filters.get("rule", "").strip(),
            "disposition": filters.get("disposition", "").strip(),
            "q": filters.get("q", "").strip(),
        }
        visible = [task for task in enriched if self._matches(task, selected, include_disposition=False)]
        calibration = self._summary(visible)
        if selected["disposition"]:
            visible = [task for task in visible if task["review_disposition"] == selected["disposition"]]
        return {
            "tasks": visible,
            "total": len(enriched),
            "visible": len(visible),
            "filters": selected,
            "active": any(selected.values()),
            "options": options,
            "calibration": calibration,
            "show_calibration": selected["family"] == "workflow_reasoning",
            "closed_count": sum(
                str(task.get("status") or "").casefold() in {"resolved", "ignored", "superseded"}
                for task in enriched
            ),
        }

    def _enrich(self, task: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(task)
        rule = str(value.get("curator_rule") or "")
        workflow_id = next(iter(value.get("related_workflows") or []), "")
        if not workflow_id and value.get("content_type") in {"workflow", "workflow_node"}:
            workflow_id = str(value.get("content_identifier") or "").split(":", 1)[0]
        target = self.lifecycle.resolve(workflow_id) if workflow_id else None
        workflow = target.workflow if target else {}
        value["workflow_id"] = workflow_id
        value["workflow_title"] = str(workflow.get("name") or workflow.get("title") or workflow_id)
        value["rule_family"] = "workflow_reasoning" if rule.startswith("CUR-WR-") else "other"
        value["rule_label"] = WorkflowReasoningAuditor.RULE_LABELS.get(rule, rule)
        value["review_disposition"] = str(value.get("review_disposition") or "NOT_REVIEWED")
        value["review_disposition_label"] = self.DISPOSITIONS.get(
            value["review_disposition"], "Not Reviewed"
        )
        return value

    @staticmethod
    def _matches(task: dict[str, Any], filters: dict[str, str], *, include_disposition: bool) -> bool:
        if (not filters["status"] and not filters["include_resolved"]
                and str(task.get("status") or "").casefold() in {"resolved", "ignored", "superseded"}):
            return False
        pairs = (
            ("status", "status"), ("classification", "classification"),
            ("workflow", "workflow_id"), ("family", "rule_family"), ("rule", "curator_rule"),
        )
        for filter_name, task_name in pairs:
            expected = filters[filter_name]
            if expected and str(task.get(task_name) or "").casefold() != expected.casefold():
                return False
        if include_disposition and filters["disposition"] and task["review_disposition"] != filters["disposition"]:
            return False
        query = filters["q"].casefold()
        if query:
            values = (
                task.get("title"), task.get("workflow_title"), task.get("curator_rule"),
                task.get("finding_id"), task.get("task_id"), task.get("content_identifier"),
            )
            if not any(query in str(value or "").casefold() for value in values):
                return False
        return True

    def _options(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        unique = lambda field: sorted({str(task.get(field) or "") for task in tasks if task.get(field)})
        workflows = sorted(
            {(task["workflow_id"], task["workflow_title"]) for task in tasks if task["workflow_id"]},
            key=lambda item: item[1].casefold(),
        )
        represented_rules = {
            task["curator_rule"] for task in tasks
            if task["rule_family"] == "workflow_reasoning"
        }
        rules = [
            (rule, label)
            for rule, label in WorkflowReasoningAuditor.RULE_LABELS.items()
            if rule in represented_rules
        ]
        return {
            "statuses": unique("status"),
            "classifications": unique("classification"),
            "workflows": workflows,
            "rules": rules,
            "dispositions": list(self.DISPOSITIONS.items()),
        }

    def _summary(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return ReasoningCalibrationService().summary(tasks)
