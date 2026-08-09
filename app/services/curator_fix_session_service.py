from __future__ import annotations

import json
import os
import re
import secrets
import time
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CuratorFixSessionError(RuntimeError):
    pass


class CuratorFixSessionService:
    """Durable, append-only maintenance-session coordination state."""

    SCHEMA_VERSION = "2.0"
    ID_PATTERN = re.compile(r"^CFX-[0-9A-F]{12}$")

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.directory = self.root / "curation_memory" / "fix_sessions"

    def create(self, *, started_by: str, originating_audit_id: str | None,
               queue: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
        reviewer = str(started_by or "").strip()
        if not reviewer:
            raise CuratorFixSessionError("Enter the reviewer who is starting this maintenance session.")
        now = datetime.now(timezone.utc).isoformat()
        try:
            baseline_fingerprint = self.fingerprint(baseline)
        except (TypeError, ValueError) as error:
            raise CuratorFixSessionError("Maintenance baseline could not be serialized safely.") from error
        session = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": f"CFX-{secrets.token_hex(6).upper()}",
            "started_by": reviewer,
            "started_at": now,
            "updated_at": now,
            "ended_at": None,
            "originating_audit_id": originating_audit_id,
            "finding_count": len(queue),
            "original_queue_count": len(queue),
            "repair_queue": deepcopy(queue),
            "outcomes": {"completed": [], "skipped": [], "deferred": [], "rejected": [],
                         "resolved_external": [], "unavailable_external": []},
            "starting_integrity": deepcopy(baseline),
            "current_integrity": deepcopy(baseline),
            "starting_debt": self.debt(baseline),
            "current_debt": self.debt(baseline),
            "debt_reduced": 0,
            "session_debt_reduced": 0,
            "external_debt_reduced": 0,
            "last_reconciled_at": now,
            "integrity_fingerprint": baseline_fingerprint,
            "health_changes": {},
            "notes": [],
            "events": [{"at": now, "event": "session_started", "actor": reviewer}],
        }
        path = self._path(session["session_id"])
        try:
            self.save(session)
            persisted = self.get(session["session_id"])
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return persisted

    def create_or_resume(self, *, started_by: str, originating_audit_id: str | None,
                         queue: list[dict[str, Any]], baseline: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create one active session per reviewer, or return their unfinished session."""
        reviewer = str(started_by or "").strip()
        if not reviewer:
            raise CuratorFixSessionError("Enter the reviewer who is starting this maintenance session.")
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / ".create.lock"
        descriptor = None
        for _ in range(20):
            try:
                descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.01)
        if descriptor is None:
            raise CuratorFixSessionError("Another maintenance session is being created. Please retry.")
        try:
            os.close(descriptor)
            active = self.find_active(reviewer)
            if active:
                return active, True
            return self.create(started_by=reviewer, originating_audit_id=originating_audit_id,
                               queue=queue, baseline=baseline), False
        finally:
            lock.unlink(missing_ok=True)

    def find_active(self, reviewer: str) -> dict[str, Any] | None:
        normalized = str(reviewer or "").strip().casefold()
        for summary in self.list_sessions():
            if summary.get("ended_at") is None and str(summary.get("started_by") or "").strip().casefold() == normalized:
                try:
                    return self.get(summary["session_id"])
                except CuratorFixSessionError:
                    continue
        return None

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.is_file():
            raise CuratorFixSessionError("Maintenance session was not found.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CuratorFixSessionError("Maintenance session could not be read safely.") from error
        if value.get("schema_version") not in {"1.0", self.SCHEMA_VERSION}:
            raise CuratorFixSessionError("Maintenance session uses an unsupported schema.")
        if value.get("schema_version") == "1.0":
            value = self._migrate(value)
            self.save(value)
        return value

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return resumable session summaries without trusting directory filenames."""
        if not self.directory.is_dir():
            return []
        sessions = []
        for path in self.directory.glob("CFX-*.json"):
            try:
                session = self.get(path.stem)
            except CuratorFixSessionError:
                continue
            progress = self.progress(session)
            sessions.append({
                "session_id": session["session_id"], "started_by": session.get("started_by"),
                "started_at": session.get("started_at"), "updated_at": session.get("updated_at"),
                "ended_at": session.get("ended_at"), "finding_count": progress["original_queue"],
                "handled": progress["handled"], "current_debt": session.get("current_debt", 0),
                "progress": progress,
            })
        return sorted(sessions, key=lambda value: value.get("updated_at") or "", reverse=True)

    def save(self, session: dict[str, Any]) -> None:
        session_id = str(session.get("session_id") or "")
        path = self._path(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        value = deepcopy(session)
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            raise CuratorFixSessionError("Maintenance session could not be saved safely.") from error

    def record(self, session_id: str, item_id: str, outcome: str, *, note: str = "",
               verification: dict[str, Any] | None = None, current: dict[str, Any] | None = None) -> dict[str, Any]:
        if outcome not in {"completed", "skipped", "deferred", "rejected"}:
            raise CuratorFixSessionError("Unsupported maintenance outcome.")
        session = self.get(session_id)
        item = next((entry for entry in session["repair_queue"] if entry["item_id"] == item_id), None)
        if not item:
            raise CuratorFixSessionError("Repair item was not found in this session.")
        for values in session["outcomes"].values():
            values[:] = [entry for entry in values if entry.get("item_id") != item_id]
        event = {"item_id": item_id, "at": datetime.now(timezone.utc).isoformat(), "note": note.strip(),
                 "verification": verification or {}}
        session["outcomes"][outcome].append(event)
        item["status"] = outcome
        if current is not None:
            session["current_integrity"] = deepcopy(current)
            self.recalculate_accounting(session)
            session["health_changes"] = self.health_changes(session["starting_integrity"], current)
            session["integrity_fingerprint"] = self.fingerprint(current)
        session["events"].append({"at": event["at"], "event": f"item_{outcome}", "item_id": item_id,
                                  "actor": session["started_by"], "note": note.strip()})
        self.save(session)
        return session

    def record_task_outcome(self, session_id: str, task_id: str, outcome: str, *,
                            note: str = "") -> dict[str, Any]:
        """Apply a task decision to the one matching open maintenance item."""
        session = self.get(session_id)
        matches = [item for item in session.get("repair_queue", [])
                   if item.get("status", "open") == "open"
                   and str(item.get("affected_content", {}).get("task_id") or "") == task_id]
        if len(matches) != 1:
            if not matches:
                raise CuratorFixSessionError(
                    "This task is not an actionable item in the current maintenance session."
                )
            raise CuratorFixSessionError(
                "This task maps to more than one maintenance item and cannot be changed safely."
            )
        return self.record(session_id, matches[0]["item_id"], outcome, note=note)

    def task_action_eligible(self, session_id: str, task_id: str) -> bool:
        session = self.get(session_id)
        return sum(
            item.get("status", "open") == "open"
            and str(item.get("affected_content", {}).get("task_id") or "") == task_id
            for item in session.get("repair_queue", [])
        ) == 1

    @staticmethod
    def recalculate_accounting(session: dict[str, Any]) -> None:
        """Derive debt attribution from queue state so retries remain idempotent."""
        starting = int(session.get("starting_debt", 0))
        current = CuratorFixSessionService.debt(session.get("current_integrity", {}))
        total_reduction = max(0, starting - current)
        completed_weight = sum(int(item.get("knowledge_debt") or 0)
                               for item in session.get("repair_queue", [])
                               if item.get("status") == "completed")
        session_reduction = min(total_reduction, completed_weight)
        session["session_debt_reduced"] = session_reduction
        session["external_debt_reduced"] = total_reduction - session_reduction
        session["debt_reduced"] = total_reduction
        session["current_debt"] = current

    @staticmethod
    def progress(session: dict[str, Any], *, category: str = "all",
                 current_item_id: str = "") -> dict[str, Any]:
        """Return the one authoritative maintenance progress view.

        ``original_queue`` is an immutable historical baseline. Findings appended by
        reconciliation are tracked explicitly as ``discovered_during_session`` and
        never rewrite that baseline. ``Repair X of Y`` uses the currently actionable
        (open) filtered queue, while debt attribution remains independent.
        """
        queue = list(session.get("repair_queue", []))
        original = int(session.get("original_queue_count",
                                   session.get("finding_count", len(queue))))
        discovered = sum(bool(item.get("introduced_after_start")) for item in queue)
        # Backward-compatible sessions may have appended work before the marker existed.
        discovered = max(discovered, max(0, len(queue) - original))
        completed = sum(item.get("status") == "completed" for item in queue)
        external = sum(item.get("status") == "resolved_external" for item in queue)
        unavailable = sum(item.get("status") == "unavailable_external" for item in queue)
        deferred = sum(item.get("status") == "deferred" for item in queue)
        skipped = sum(item.get("status") == "skipped" for item in queue)
        rejected = sum(item.get("status") == "rejected" for item in queue)
        open_items = [item for item in queue if item.get("status", "open") == "open"]
        if category != "all":
            open_items = [item for item in open_items if item.get("finding_type") == category]
        current_position = next(
            (index for index, item in enumerate(open_items, 1)
             if item.get("item_id") == current_item_id),
            1 if open_items else 0,
        )
        return {
            "original_queue": original,
            "discovered_during_session": discovered,
            "total_tracked": len(queue),
            "current_actionable": sum(item.get("status", "open") == "open" for item in queue),
            "filtered_actionable": len(open_items),
            "current_position": current_position,
            "session_repairs": completed,
            "external_resolutions": external,
            "external_unavailable": unavailable,
            "deferred": deferred,
            "remaining": sum(item.get("status", "open") == "open" for item in queue),
            "handled": completed + external + unavailable + deferred + skipped + rejected,
            "starting_debt": int(session.get("starting_debt", 0)),
            "current_debt": int(session.get("current_debt", 0)),
            "session_reduction": int(session.get("session_debt_reduced", 0)),
            "external_reduction": int(session.get("external_debt_reduced", 0)),
            "total_reduction": int(session.get("debt_reduced", 0)),
        }

    def finish(self, session_id: str, current: dict[str, Any]) -> dict[str, Any]:
        session = self.get(session_id)
        session["ended_at"] = datetime.now(timezone.utc).isoformat()
        session["current_integrity"] = deepcopy(current)
        session["current_debt"] = self.debt(current)
        session["debt_reduced"] = max(0, session["starting_debt"] - session["current_debt"])
        session["external_debt_reduced"] = max(
            0, session["debt_reduced"] - int(session.get("session_debt_reduced", 0)))
        session["health_changes"] = self.health_changes(session["starting_integrity"], current)
        session["events"].append({"at": session["ended_at"], "event": "session_completed",
                                  "actor": session["started_by"]})
        self.save(session)
        return session

    @staticmethod
    def debt(report: dict[str, Any]) -> int:
        counts = report.get("counts", report)
        return (
            int(counts.get("broken_relationships", 0)) * 10
            + int(counts.get("duplicate_groups", 0)) * 8
            + int(counts.get("inventory_mismatches", 0)) * 4
            + int(counts.get("missing_review_metadata", 0)) * 2
            + int(counts.get("orphaned_articles", 0))
        )

    @staticmethod
    def health_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, int]]:
        keys = ("broken_relationships", "duplicate_groups", "inventory_mismatches",
                "orphaned_articles", "missing_review_metadata")
        old = before.get("counts", before)
        new = after.get("counts", after)
        return {key: {"before": int(old.get(key, 0)), "after": int(new.get(key, 0)),
                      "change": int(new.get(key, 0)) - int(old.get(key, 0))} for key in keys}

    @staticmethod
    def fingerprint(report: dict[str, Any]) -> str:
        payload = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _migrate(self, session: dict[str, Any]) -> dict[str, Any]:
        """Upgrade persisted 1.0 sessions without changing their original baseline."""
        value = deepcopy(session)
        now = datetime.now(timezone.utc).isoformat()
        value["schema_version"] = self.SCHEMA_VERSION
        value.setdefault("original_queue_count", value.get("finding_count", len(value.get("repair_queue", []))))
        value.setdefault("session_debt_reduced", int(value.get("debt_reduced", 0)))
        value.setdefault("external_debt_reduced", 0)
        value.setdefault("outcomes", {}).setdefault("resolved_external", [])
        value.setdefault("outcomes", {}).setdefault("unavailable_external", [])
        value.setdefault("last_reconciled_at", None)
        value.setdefault("integrity_fingerprint", self.fingerprint(value.get("current_integrity", {})))
        for item in value.get("repair_queue", []):
            item.setdefault("original_snapshot", deepcopy({key: entry for key, entry in item.items()
                                                            if key != "original_snapshot"}))
            item.setdefault("latest_snapshot", deepcopy({key: entry for key, entry in item.items()
                                                          if key not in {"original_snapshot", "latest_snapshot"}}))
            item.setdefault("classification_history", [])
            item.setdefault("status_history", [])
        value.setdefault("events", []).append({"at": now, "event": "session_schema_migrated",
                                                "from": "1.0", "to": self.SCHEMA_VERSION})
        return value

    def _path(self, session_id: str) -> Path:
        if not self.ID_PATTERN.fullmatch(str(session_id or "")):
            raise CuratorFixSessionError("Maintenance session ID is invalid.")
        return self.directory / f"{session_id}.json"
