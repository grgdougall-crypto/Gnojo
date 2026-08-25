from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.services.curator_structural_repair_governance import StructuralRepairFingerprint


DRAFT_PERSISTENCE_FAILURES = frozenset({
    "draft_not_found",
    "invalid_draft_path",
    "lock_unavailable",
    "stale_workflow",
    "persistence_failed",
    "verification_failed",
    "restore_failed",
})


class WorkflowDraftPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str):
        if code not in DRAFT_PERSISTENCE_FAILURES:
            raise ValueError("Workflow draft persistence error code is unsupported.")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkflowDraftSnapshot:
    filename: str
    content: bytes
    raw_sha256: str
    semantic_sha256: str
    workflow: dict[str, Any]


@dataclass(frozen=True)
class WorkflowDraftReplacement:
    before: WorkflowDraftSnapshot
    after: WorkflowDraftSnapshot


class _OperatingSystemFileLock:
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
                    raise WorkflowDraftPersistenceError(
                        "lock_unavailable", "The editable workflow draft is currently being updated."
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


class LockedWorkflowDraft:
    def __init__(self, owner: "WorkflowDraftPersistence", filename: str, path: Path):
        self.owner = owner
        self.filename = filename
        self.path = path

    def read(self) -> WorkflowDraftSnapshot:
        if not self.path.is_file():
            raise WorkflowDraftPersistenceError("draft_not_found", "Editable workflow draft was not found.")
        try:
            content = self.owner._read_bytes(self.path)
            return self.owner._snapshot(self.filename, content)
        except WorkflowDraftPersistenceError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise WorkflowDraftPersistenceError(
                "persistence_failed", "Editable workflow draft could not be read safely."
            ) from error

    def replace(self, expected_raw_sha256: str, replacement: bytes | dict[str, Any]) -> WorkflowDraftReplacement:
        before = self.read()
        expected = str(expected_raw_sha256 or "").strip().lower()
        if before.raw_sha256 != expected:
            raise WorkflowDraftPersistenceError(
                "stale_workflow", "Editable workflow draft changed after it was inspected."
            )
        try:
            replacement_bytes = self.owner._replacement_bytes(replacement)
            proposed = self.owner._snapshot(self.filename, replacement_bytes)
        except WorkflowDraftPersistenceError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise WorkflowDraftPersistenceError(
                "persistence_failed", "Replacement workflow content is invalid."
            ) from error
        if proposed.workflow.get("workflow_id") != before.workflow.get("workflow_id"):
            raise WorkflowDraftPersistenceError(
                "invalid_draft_path", "Replacement workflow identity does not match the editable draft."
            )
        try:
            self.owner._atomic_replace(self.path, replacement_bytes)
        except WorkflowDraftPersistenceError:
            raise
        except OSError as error:
            raise WorkflowDraftPersistenceError(
                "persistence_failed", "Editable workflow draft could not be replaced atomically."
            ) from error
        try:
            persisted = self.owner._snapshot(self.filename, self.owner._read_bytes(self.path))
        except Exception as error:
            raise WorkflowDraftPersistenceError(
                "verification_failed", "Persisted workflow draft could not be verified."
            ) from error
        if persisted.content != replacement_bytes or persisted.raw_sha256 != proposed.raw_sha256:
            raise WorkflowDraftPersistenceError(
                "verification_failed", "Persisted workflow draft differs from the requested replacement."
            )
        return WorkflowDraftReplacement(before=before, after=persisted)

    def restore(self, expected_current_raw_sha256: str, backup: bytes) -> WorkflowDraftReplacement:
        try:
            return self.replace(expected_current_raw_sha256, backup)
        except WorkflowDraftPersistenceError as error:
            if error.code == "stale_workflow":
                raise
            raise WorkflowDraftPersistenceError(
                "restore_failed", "The exact workflow draft backup could not be restored safely."
            ) from error
        except Exception as error:
            raise WorkflowDraftPersistenceError(
                "restore_failed", "The exact workflow draft backup could not be restored safely."
            ) from error

    def create_or_replace(self, replacement: bytes | dict[str, Any]) -> WorkflowDraftSnapshot:
        replacement_bytes = self.owner._replacement_bytes(replacement)
        proposed = self.owner._snapshot(self.filename, replacement_bytes)
        expected_filename = f"{proposed.workflow.get('workflow_id')}.json"
        if expected_filename != self.filename:
            raise WorkflowDraftPersistenceError(
                "invalid_draft_path", "Workflow identity does not match the editable draft filename."
            )
        if self.path.exists():
            return self.replace(self.read().raw_sha256, replacement_bytes).after
        try:
            self.owner._atomic_replace(self.path, replacement_bytes)
            persisted = self.owner._snapshot(self.filename, self.owner._read_bytes(self.path))
        except Exception as error:
            if isinstance(error, WorkflowDraftPersistenceError):
                raise
            raise WorkflowDraftPersistenceError(
                "persistence_failed", "Editable workflow draft could not be created safely."
            ) from error
        if persisted.content != replacement_bytes:
            raise WorkflowDraftPersistenceError(
                "verification_failed", "Created workflow draft differs from the requested content."
            )
        return persisted


class WorkflowDraftPersistence:
    """Shared OS-locking and exact-byte CAS boundary for editable workflow drafts."""

    def __init__(self, drafts_path: Path):
        self.drafts_path = Path(drafts_path).resolve()
        self.drafts_path.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.drafts_path.parent / ".workflow_draft_locks"

    @contextmanager
    def locked(self, filename: str, *, timeout: float = 2.0) -> Iterator[LockedWorkflowDraft]:
        filename, path = self._target(filename)
        lock = _OperatingSystemFileLock(self.lock_path / f"{filename}.lock", timeout)
        lock.acquire()
        try:
            yield LockedWorkflowDraft(self, filename, path)
        finally:
            lock.release()

    def compare_and_swap(self, filename: str, expected_raw_sha256: str,
                         replacement: bytes | dict[str, Any], *, timeout: float = 2.0
                         ) -> WorkflowDraftReplacement:
        with self.locked(filename, timeout=timeout) as draft:
            return draft.replace(expected_raw_sha256, replacement)

    def save(self, filename: str, replacement: bytes | dict[str, Any], *, timeout: float = 2.0
             ) -> WorkflowDraftSnapshot:
        with self.locked(filename, timeout=timeout) as draft:
            return draft.create_or_replace(replacement)

    def _target(self, filename: str) -> tuple[str, Path]:
        value = str(filename or "").strip()
        if not value or Path(value).name != value or not value.endswith(".json"):
            raise WorkflowDraftPersistenceError("invalid_draft_path", "Workflow draft filename is invalid.")
        path = (self.drafts_path / value).resolve()
        if path.parent != self.drafts_path:
            raise WorkflowDraftPersistenceError("invalid_draft_path", "Workflow draft path is outside the editable store.")
        return value, path

    @staticmethod
    def _replacement_bytes(value: bytes | dict[str, Any]) -> bytes:
        if isinstance(value, bytes):
            return value
        if not isinstance(value, dict):
            raise WorkflowDraftPersistenceError("persistence_failed", "Replacement workflow must be bytes or an object.")
        return (json.dumps(value, indent=4, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _snapshot(filename: str, content: bytes) -> WorkflowDraftSnapshot:
        workflow = json.loads(content.decode("utf-8"))
        if not isinstance(workflow, dict):
            raise ValueError("Workflow draft must contain a JSON object.")
        return WorkflowDraftSnapshot(
            filename=filename,
            content=content,
            raw_sha256=StructuralRepairFingerprint.raw_workflow(content),
            semantic_sha256=StructuralRepairFingerprint.semantic_workflow(workflow),
            workflow=workflow,
        )

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        return path.read_bytes()

    def _atomic_replace(self, path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
