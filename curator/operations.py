from __future__ import annotations

from datetime import datetime
from typing import Any

from .debt import KnowledgeDebtService
from .health import KnowledgeHealthService
from .learning import CuratorLearningService
from .memory import CuratorMemoryStore
from .models import AuditResult
from .tasks import KnowledgeTaskService


class KnowledgeOperationsService:
    def __init__(self, store: CuratorMemoryStore):
        self.store = store

    def process(self, result: AuditResult) -> None:
        state = self.store.load()
        previous = state.get("audits", [])[-1] if state.get("audits") else {}
        reconciliation = KnowledgeTaskService().reconcile(
            state, result.findings, result.inventory, run_id=result.run_id,
            observed_at=result.completed_at, filters=result.filters,
        )
        debt = KnowledgeDebtService().calculate(state["tasks"], previous.get("knowledge_debt"))
        health = KnowledgeHealthService().calculate(result.inventory, result.findings, previous.get("knowledge_health"))
        learning = CuratorLearningService().analyze(state["tasks"])
        proposed_lessons = CuratorLearningService().persist_proposed_lessons(
            state, learning, observed_at=result.completed_at,
        )
        task_snapshot = {
            "summary": {
                "total": len(state["tasks"]),
                "open": sum(task.get("status") == "open" for task in state["tasks"].values()),
                "in_progress": sum(task.get("status") == "in_progress" for task in state["tasks"].values()),
                "resolved": sum(task.get("status") == "resolved" for task in state["tasks"].values()),
                "ignored": sum(task.get("status") == "ignored" for task in state["tasks"].values()),
                "created_this_run": len(reconciliation["created"]),
                "resolved_this_run": len(reconciliation["resolved"]),
                "returned_this_run": len(reconciliation["returned"]),
            },
            "tasks": sorted(state["tasks"].values(), key=lambda item: item["task_id"]),
        }
        memory_summary = {
            "schema_version": state["schema_version"],
            "audit_number": len(state.get("audits", [])) + 1,
            "previous_run_id": previous.get("run_id"),
            "is_filtered_audit": not KnowledgeTaskService._is_full_audit(result.filters),
            "note": (
                "Filtered audits observe matching content but do not resolve tasks outside their scope."
                if not KnowledgeTaskService._is_full_audit(result.filters)
                else "Complete audit: active tasks not observed in this run may be resolved deterministically."
            ),
        }
        result.knowledge_tasks = task_snapshot
        result.knowledge_debt = debt
        result.knowledge_health = health
        result.lessons_learned = learning
        result.memory_summary = memory_summary
        try:
            duration_seconds = max(0.0, (
                datetime.fromisoformat(result.completed_at)
                - datetime.fromisoformat(result.started_at)
            ).total_seconds())
        except (TypeError, ValueError):
            duration_seconds = None
        state.setdefault("audits", []).append({
            "run_id": result.run_id,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_seconds": duration_seconds,
            "auditor_version": result.auditor_version,
            "filters": result.filters.to_dict(),
            "summary": result.summary(),
            "knowledge_debt": debt,
            "knowledge_health": health,
            "task_changes": {key: reconciliation[key] for key in ("created", "resolved", "returned")},
            "proposed_lessons": proposed_lessons,
        })
        self.store.save(state)
