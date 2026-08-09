from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_repair_planner import CuratorRepairPlanner
from app.services.curator_task_reconciliation_service import CuratorTaskReconciliationService
from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from curator.memory import CuratorMemoryStore


class CuratorSessionReconciliationService:
    """Refresh unresolved maintenance conclusions against current repository truth."""

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.sessions = CuratorFixSessionService(self.root)
        self.integrity = KnowledgeIntegrityService(self.root)
        self.planner = CuratorRepairPlanner(self.root)
        self.tasks = CuratorTaskReconciliationService(self.root)
        self.memory = CuratorMemoryStore(self.root / "curation_memory")

    def reconcile(self, session_id: str, *, trigger: str = "resume") -> dict[str, Any]:
        session = self.sessions.get(session_id)
        current_integrity = self.integrity.report()
        current_items = {item["item_id"]: item for item in self.planner.build(current_integrity)}
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        externally_resolved = 0
        externally_synchronized = 0
        task_state_changes: dict[str, int] = {}
        task_records = self.memory.load().get("tasks", {})

        known_ids = set()
        for item in session.get("repair_queue", []):
            item_id = item["item_id"]
            known_ids.add(item_id)
            status = item.get("status", "open")
            latest = current_items.get(item_id)
            item.setdefault("original_snapshot", deepcopy(self._snapshot(item)))
            item.setdefault("classification_history", [])
            item.setdefault("status_history", [])

            task_id = str(item.get("affected_content", {}).get("task_id") or "")
            if status == "open" and task_id:
                task = task_records.get(task_id)
                authoritative = self._task_outcome(task, session_id=session_id)
                if authoritative:
                    outcome, task_status = authoritative
                    self._record_external_task_state(
                        session, item, outcome=outcome, task_id=task_id,
                        task_status=task_status, trigger=trigger, at=now,
                    )
                    status = outcome
                    changed = True
                    externally_synchronized += 1
                    task_state_changes[task_status] = task_state_changes.get(task_status, 0) + 1

            if latest is None:
                if status == "open":
                    previous = item.get("classification")
                    item["latest_snapshot"] = deepcopy(self._snapshot(item))
                    task = task_records.get(task_id, {}) if task_id else {}
                    resolution_session = str(task.get("resolution_metadata", {}).get("maintenance_session_id") or "")
                    resolved_in_session = task.get("status") == "resolved" and resolution_session == session_id
                    resolved_status = "completed" if resolved_in_session else "resolved_external"
                    item["status"] = resolved_status
                    item["resolved_externally_at"] = now
                    item["external_resolution_evidence"] = {
                        "reason": "The finding no longer exists in the current targeted integrity report.",
                        "verified": True,
                        "trigger": trigger,
                    }
                    item["status_history"].append({"at": now, "from": status,
                                                   "to": resolved_status, "trigger": trigger})
                    outcome = "completed" if resolved_in_session else "resolved_external"
                    outcomes = session.setdefault("outcomes", {}).setdefault(outcome, [])
                    if not any(entry.get("item_id") == item_id for entry in outcomes):
                        outcomes.append({
                        "item_id": item_id, "at": now,
                        "note": ("Resolved by an explicit human task decision in this maintenance session."
                                 if resolved_in_session else
                                 "Resolved outside this maintenance session and verified during reconciliation."),
                        "verification": {"verified": True, "previous_classification": previous,
                                         "task_id": task_id, "resolution_session": resolution_session},
                    })
                    session.setdefault("events", []).append({
                        "at": now, "event": ("item_resolved_by_task" if resolved_in_session else
                                               "item_resolved_externally"), "item_id": item_id,
                        "previous_classification": previous, "trigger": trigger,
                    })
                    if not resolved_in_session:
                        self.tasks.reconcile_external(item, session_id=session_id,
                                                      reason="Finding resolved outside the Fix Wizard.")
                        externally_resolved += 1
                    changed = True
                continue

            if status == "completed" and not item.get("external_task_state") and item.get("finding_type") in {
                "broken_relationship", "inventory_mismatch", "duplicate_group", "legacy_provenance"
            }:
                # A current deterministic finding with the same stable identity is a true recurrence.
                item["recurrence_count"] = int(item.get("recurrence_count", 0)) + 1
                item["status"] = "open"
                item["status_history"].append({"at": now, "from": "completed", "to": "open",
                                               "reason": "Verified finding recurred", "trigger": trigger})
                session.setdefault("events", []).append({"at": now, "event": "finding_recurred",
                                                          "item_id": item_id, "trigger": trigger})
                status = "open"
                changed = True

            if status in {"completed", "skipped", "deferred", "rejected", "resolved_external",
                          "unavailable_external"}:
                # Preserve explicit review decisions. Editorial work must not be silently
                # reopened merely because its observation remains visible in a later audit.
                item["latest_snapshot"] = deepcopy(self._snapshot(latest))
                item["last_reconciled_at"] = now
                continue

            previous_classification = item.get("classification")
            preserved = {key: item.get(key) for key in (
                "original_snapshot", "classification_history", "status_history", "status",
                "resolved_externally_at", "external_resolution_evidence", "recurrence_count"
            ) if key in item}
            item.update(deepcopy(latest))
            item.update(preserved)
            item["latest_snapshot"] = deepcopy(self._snapshot(latest))
            item["last_reconciled_at"] = now
            if previous_classification != latest.get("classification"):
                reason = self._change_reason(previous_classification, latest.get("classification"))
                item["previous_classification"] = previous_classification
                item["classification_change_reason"] = reason
                item["classification_history"].append({
                    "at": now, "from": previous_classification, "to": latest.get("classification"),
                    "reason": reason, "trigger": trigger,
                })
                session.setdefault("events", []).append({
                    "at": now, "event": "item_reclassified", "item_id": item_id,
                    "from": previous_classification, "to": latest.get("classification"),
                    "reason": reason, "trigger": trigger,
                })
                self.tasks.reconcile_classification(item, session_id=session_id,
                                                    previous=previous_classification,
                                                    current=latest.get("classification"))
                changed = True

        # Genuine new findings join the current queue without changing the original baseline/count.
        for item_id, item in current_items.items():
            if item_id in known_ids:
                continue
            value = deepcopy(item)
            value["introduced_after_start"] = True
            value["original_snapshot"] = deepcopy(self._snapshot(value))
            value["latest_snapshot"] = deepcopy(self._snapshot(value))
            value["classification_history"] = []
            value["status_history"] = [{"at": now, "from": None, "to": "open", "trigger": trigger}]
            session["repair_queue"].append(value)
            session.setdefault("events", []).append({"at": now, "event": "finding_added_after_start",
                                                      "item_id": item_id, "trigger": trigger})
            changed = True

        previous_fingerprint = session.get("integrity_fingerprint")
        current_fingerprint = self.sessions.fingerprint(current_integrity)
        session["repository_changes_detected"] = previous_fingerprint != current_fingerprint
        session["current_integrity"] = deepcopy(current_integrity)
        self.sessions.recalculate_accounting(session)
        session["health_changes"] = self.sessions.health_changes(session.get("starting_integrity", {}), current_integrity)
        session["integrity_fingerprint"] = current_fingerprint
        session["last_reconciled_at"] = now
        # finding_count is a legacy persisted field. Do not rewrite it in an active
        # session; progress() supplies the authoritative, explicitly labelled model.
        session["reconciliation_summary"] = self.sessions.progress(session)
        session["last_reconciliation"] = {
            "at": now,
            "trigger": trigger,
            "changed": changed,
            "externally_synchronized": externally_synchronized,
            "task_state_changes": task_state_changes,
        }
        session.setdefault("events", []).append({
            "at": now, "event": "session_reconciled", "trigger": trigger,
            "repository_changed": previous_fingerprint != current_fingerprint,
            "externally_resolved": externally_resolved,
            "externally_synchronized": externally_synchronized,
            "changed": changed,
        })
        self.sessions.save(session)
        return self.sessions.get(session_id)

    @staticmethod
    def _task_outcome(task: dict[str, Any] | None, *, session_id: str) -> tuple[str, str] | None:
        """Map authoritative non-actionable task state to a precise queue outcome."""
        if task is None:
            return "unavailable_external", "missing"
        status = str(task.get("status") or "").strip().casefold()
        if status == "deferred":
            return "deferred", status
        if status == "ignored":
            return "rejected", status
        if status == "resolved":
            resolution_session = str(task.get("resolution_metadata", {}).get("maintenance_session_id") or "")
            return ("completed" if resolution_session == session_id else "resolved_external"), status
        if status == "superseded":
            return "unavailable_external", status
        if status not in {"open", "in_progress"}:
            return "unavailable_external", status or "unknown"
        return None

    @staticmethod
    def _record_external_task_state(session: dict[str, Any], item: dict[str, Any], *,
                                    outcome: str, task_id: str, task_status: str,
                                    trigger: str, at: str) -> None:
        """Synchronize one open item without fabricating a Fix Wizard reviewer action."""
        item_id = item["item_id"]
        previous = item.get("status", "open")
        item["status"] = outcome
        item["external_task_state"] = {
            "task_id": task_id, "status": task_status, "observed_at": at, "trigger": trigger,
        }
        item["status_history"].append({
            "at": at, "from": previous, "to": outcome,
            "reason": f"Authoritative Knowledge Task state is {task_status}.",
            "trigger": trigger, "source": "knowledge_task",
        })
        for values in session.setdefault("outcomes", {}).values():
            values[:] = [entry for entry in values if entry.get("item_id") != item_id]
        outcomes = session["outcomes"].setdefault(outcome, [])
        outcomes.append({
            "item_id": item_id, "at": at,
            "note": f"Synchronized from authoritative Knowledge Task state '{task_status}'.",
            "verification": {
                "source": "knowledge_task", "task_id": task_id,
                "task_status": task_status, "external": True,
            },
        })
        session.setdefault("events", []).append({
            "at": at, "event": "item_synchronized_from_task", "item_id": item_id,
            "task_id": task_id, "task_status": task_status, "outcome": outcome,
            "trigger": trigger,
        })

    @staticmethod
    def _snapshot(item: dict[str, Any]) -> dict[str, Any]:
        excluded = {"original_snapshot", "latest_snapshot", "classification_history", "status_history"}
        return {key: deepcopy(value) for key, value in item.items() if key not in excluded}

    @staticmethod
    def _change_reason(previous: str | None, current: str | None) -> str:
        if previous == "CREATE_ARTICLE_REQUIRED" and current == "RELINK_EXISTING":
            return "A canonical published article became available after this maintenance session started."
        if previous == "CREATE_ARTICLE_REQUIRED" and current == "LIKELY_MATCH_REVIEW":
            return "A viable published article candidate became available; human identity review is required."
        return "Current repository evidence changed the repair classification."

    @staticmethod
    def _summary(session: dict[str, Any]) -> dict[str, int]:
        # Kept for callers from older extensions; all arithmetic lives in one service.
        return CuratorFixSessionService.progress(session)
