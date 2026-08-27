from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from curator.calibration import ReasoningCalibrationService


MEMORY_SCHEMA_VERSION = "1.0"


def _growth_defaults() -> dict[str, Any]:
    return {"lessons": {}, "proposals": {}, "evaluations": {}, "event_queue": []}


def _control_defaults() -> dict[str, Any]:
    return {
        "global_disabled": False,
        "scheduled_runs_disabled": True,
        "stage_b_scheduled_runs_disabled": True,
    }


class CuratorMemoryError(RuntimeError):
    pass


class CuratorMemoryConflictError(CuratorMemoryError):
    """Curator memory changed after a writer read its precondition state."""


class CuratorMemoryLockError(CuratorMemoryError):
    """The shared Curator-memory writer lock could not be acquired."""


class CuratorMemoryState(dict):
    """Dictionary-compatible state carrying its exact persisted precondition."""

    def __init__(self, value: dict[str, Any], fingerprint: str):
        super().__init__(value)
        self._curator_memory_fingerprint = fingerprint


@dataclass(frozen=True)
class CuratorMemorySnapshot:
    state: CuratorMemoryState
    fingerprint: str


class _CuratorMemoryFileLock:
    def __init__(self, path: Path, timeout: float, poll_interval: float = 0.025):
        self.path = path
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.001, float(poll_interval))
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock_nonblocking()
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise CuratorMemoryLockError(
                        "Curator memory is currently being updated by another process."
                    )
                time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if not self._file:
            return
        try:
            self._unlock()
        finally:
            self._file.close()
            self._file = None

    def _lock_nonblocking(self) -> None:
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)


class LockedCuratorMemory:
    def __init__(self, store: "CuratorMemoryStore"):
        self.store = store

    def snapshot(self) -> CuratorMemorySnapshot:
        return self.store._snapshot()

    def compare_and_swap(
        self, expected_fingerprint: str, state: dict[str, Any], *,
        touch_updated_at: bool = True,
    ) -> CuratorMemorySnapshot:
        current = self.snapshot()
        if current.fingerprint != str(expected_fingerprint or ""):
            raise CuratorMemoryConflictError(
                "Curator memory changed after it was inspected."
            )
        return self.store._write_locked(state, touch_updated_at=touch_updated_at)


class CuratorMemoryStore:
    """Durable operational memory. This store never writes trusted content."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state_path = self.root / "memory.json"
        self.lock_path = self.root / ".memory.lock"

    def load(self) -> dict[str, Any]:
        return self._snapshot().state

    def snapshot(self) -> CuratorMemorySnapshot:
        return self._snapshot()

    @contextmanager
    def locked(self, *, timeout: float = 2.0) -> Iterator[LockedCuratorMemory]:
        lock = _CuratorMemoryFileLock(self.lock_path, timeout)
        lock.acquire()
        try:
            yield LockedCuratorMemory(self)
        finally:
            lock.release()

    def _snapshot(self) -> CuratorMemorySnapshot:
        if not self.state_path.exists():
            value = self._default_state()
            fingerprint = self.fingerprint_bytes(None)
            return CuratorMemorySnapshot(CuratorMemoryState(value, fingerprint), fingerprint)
        try:
            content = self.state_path.read_bytes()
            value = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CuratorMemoryError(f"Unable to read Curator memory: {error}") from error
        value = self._normalize(value)
        fingerprint = self.fingerprint_bytes(content)
        return CuratorMemorySnapshot(CuratorMemoryState(value, fingerprint), fingerprint)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "created_at": now,
            "updated_at": now,
            "tasks": {},
            "audits": [],
            "decisions": [],
            "growth": _growth_defaults(),
            "controls": _control_defaults(),
        }

    @staticmethod
    def _normalize(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CuratorMemoryError("Curator memory must contain a JSON object.")
        if value.get("schema_version") != MEMORY_SCHEMA_VERSION:
            raise CuratorMemoryError(
                f"Unsupported Curator memory schema: {value.get('schema_version')!r}"
            )
        value.setdefault("tasks", {})
        value.setdefault("audits", [])
        value.setdefault("decisions", [])
        growth = value.setdefault("growth", _growth_defaults())
        for key, default in _growth_defaults().items():
            growth.setdefault(key, deepcopy(default))
        controls = value.setdefault("controls", _control_defaults())
        for key, default in _control_defaults().items():
            controls.setdefault(key, default)
        return value

    def save(
        self, state: dict[str, Any], *, expected_fingerprint: str | None = None
    ) -> None:
        expected = expected_fingerprint
        if expected is None:
            expected = getattr(state, "_curator_memory_fingerprint", None)
        if expected is None:
            expected = self.fingerprint_bytes(None) if not self.state_path.exists() else ""
        with self.locked() as memory:
            persisted = memory.compare_and_swap(expected, state)
        if isinstance(state, CuratorMemoryState):
            state._curator_memory_fingerprint = persisted.fingerprint

    def _write_locked(
        self, state: dict[str, Any], *, touch_updated_at: bool = True
    ) -> CuratorMemorySnapshot:
        self.root.mkdir(parents=True, exist_ok=True)
        value = deepcopy(state)
        value["schema_version"] = MEMORY_SCHEMA_VERSION
        if touch_updated_at:
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp", dir=self.root
        )
        try:
            content = (
                json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
            persisted = self._snapshot()
            if persisted.fingerprint != self.fingerprint_bytes(content):
                raise CuratorMemoryError("Persisted Curator memory failed verification.")
            return persisted
        except Exception as error:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            if isinstance(error, CuratorMemoryError):
                raise
            raise CuratorMemoryError(f"Unable to save Curator memory: {error}") from error

    @staticmethod
    def fingerprint_bytes(content: bytes | None) -> str:
        marker = b"CURATOR_MEMORY_ABSENT" if content is None else content
        return hashlib.sha256(marker).hexdigest()

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        priority: str | None = None,
        actor: str = "Human",
        note: str = "",
        event_name: str = "updated",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        task = state["tasks"].get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        allowed_statuses = {"open", "in_progress", "deferred", "resolved", "ignored", "superseded"}
        allowed_owners = {"Curator", "Researcher", "Workflow Designer", "Script Engineer", "QA Reviewer", "Human"}
        allowed_priorities = {"Critical", "High", "Medium", "Low", "Info"}
        if status and status not in allowed_statuses:
            raise CuratorMemoryError(f"Unsupported task status: {status}")
        if owner and owner not in allowed_owners:
            raise CuratorMemoryError(f"Unsupported task owner: {owner}")
        if priority and priority not in allowed_priorities:
            raise CuratorMemoryError(f"Unsupported task priority: {priority}")
        current_status = task.get("status", "open")
        allowed_transitions = {
            "open": {"in_progress", "deferred", "ignored", "resolved"},
            "in_progress": {"open", "deferred", "ignored", "resolved"},
            "deferred": {"open", "in_progress", "ignored", "resolved"},
            "ignored": {"open"},
            "resolved": {"open"},
            "superseded": set(),
        }
        if status == current_status:
            # A retried/double-submitted state change is already complete. Do not
            # duplicate decisions, resolution events, or debt attribution.
            return deepcopy(task)
        if status and status not in allowed_transitions.get(current_status, set()):
            raise CuratorMemoryError(
                f"Task cannot move from {current_status.replace('_', ' ')} to {status.replace('_', ' ')}."
            )
        before = {"status": task["status"], "owner": task["owner"], "priority": task["priority"]}
        if status:
            task["status"] = status
        if owner:
            task["owner"] = owner
        if priority:
            task["priority"] = priority
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event_name,
            "before": before,
            "after": {"status": task["status"], "owner": task["owner"], "priority": task["priority"]},
            "note": note,
        }
        if metadata:
            event["metadata"] = deepcopy(metadata)
        if status == "resolved":
            task["resolved_at"] = event["at"]
            task["resolution_notes"] = note
            if metadata:
                task["resolution_metadata"] = deepcopy(metadata)
        elif status == "open" and event_name == "reopen":
            task.pop("resolved_at", None)
            task.pop("resolution_metadata", None)
        task.setdefault("history", []).append(event)
        task.setdefault("resolution_history", []).append(event)
        state["decisions"].append({"task_id": task_id, **event})
        self.save(state)
        return deepcopy(task)

    def record_verification(self, task_id: str, verification: dict[str, Any]) -> dict[str, Any]:
        """Persist a read-only targeted observation without changing task lifecycle."""
        state = self.load()
        task = state["tasks"].get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        value = deepcopy(verification)
        task["current_verification"] = value
        event = {
            "at": value.get("verified_at") or datetime.now(timezone.utc).isoformat(),
            "actor": "Curator",
            "event": "targeted_verification",
            "verification_result": value.get("status"),
            "rule": value.get("rule"),
            "workflow_id": value.get("workflow_id"),
            "node_id": value.get("node_id"),
        }
        # Repeated verification of the same affected fingerprint/result is idempotent.
        history = task.setdefault("history", [])
        previous = next((item for item in reversed(history)
                         if item.get("event") == "targeted_verification"), None)
        if not previous or previous.get("verification_result") != event["verification_result"] or \
                task.get("last_verified_fingerprint") != value.get("affected_fingerprint"):
            history.append(event)
            state.setdefault("decisions", []).append({"task_id": task_id, **event})
        task["last_verified_fingerprint"] = value.get("affected_fingerprint")
        self.save(state)
        return deepcopy(task)

    def update_review_disposition(
        self, task_id: str, disposition: str, *, actor: str = "Human"
    ) -> dict[str, Any]:
        """Record calibration metadata without changing task lifecycle or content."""
        allowed = {"NOT_REVIEWED", "USEFUL", "INTENTIONAL", "FALSE_POSITIVE"}
        if disposition not in allowed:
            raise CuratorMemoryError(f"Unsupported review disposition: {disposition}")
        state = self.load()
        task = state.get("tasks", {}).get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        if not str(task.get("curator_rule") or "").startswith("CUR-WR-"):
            raise CuratorMemoryError("Review disposition is only available for workflow-reasoning tasks.")
        before = str(task.get("review_disposition") or "NOT_REVIEWED")
        if before == disposition:
            return deepcopy(task)
        reviewed_at = datetime.now(timezone.utc).isoformat()
        task["review_disposition"] = disposition
        calibration = ReasoningCalibrationService().snapshot(
            task, disposition, reviewed_at=reviewed_at
        )
        task["reasoning_calibration"] = calibration
        event = {
            "at": reviewed_at,
            "actor": actor,
            "event": "reasoning_review_disposition",
            "before": before,
            "after": disposition,
            "calibration": deepcopy(calibration),
        }
        task.setdefault("history", []).append(event)
        state.setdefault("decisions", []).append({"task_id": task_id, **event})
        self.save(state)
        return deepcopy(task)
