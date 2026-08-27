from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .observation_models import ObservationRunResult


class ObservationRepositoryError(RuntimeError):
    pass


class ObservationOverlapError(RuntimeError):
    def __init__(self, owner: dict[str, Any] | None = None):
        super().__init__("Another repository observation is already running.")
        self.owner = owner or {}


class ObservationResultRepository:
    """Atomic operational results kept outside Curator and trusted content stores."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def create(self, result: ObservationRunResult) -> Path:
        directory = self.root / result.run_id
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise ObservationRepositoryError(
                f"Observation run already exists: {result.run_id}"
            ) from error
        path = directory / "result.json"
        self._atomic_write(path, result.to_dict())
        return path

    def update(self, result: ObservationRunResult) -> Path:
        path = self.root / result.run_id / "result.json"
        if not path.parent.is_dir():
            raise ObservationRepositoryError(
                f"Observation run does not exist: {result.run_id}"
            )
        self._atomic_write(path, result.to_dict())
        return path

    def get(self, run_id: str) -> ObservationRunResult | None:
        path = self.root / str(run_id) / "result.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return ObservationRunResult.from_dict(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ObservationRepositoryError("Observation result is unreadable.") from error

    def list_recent(self, limit: int = 20) -> tuple[ObservationRunResult, ...]:
        if not self.root.is_dir():
            return ()
        values = []
        for path in sorted(self.root.glob("*/result.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                values.append(ObservationRunResult.from_dict(value))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if len(values) >= max(1, min(int(limit), 100)):
                break
        return tuple(values)

    def mark_interrupted_orphan(
        self,
        owner: dict[str, Any],
        *,
        detected_at: str,
        recovery_run_id: str,
    ) -> bool:
        """Close only the exact RUNNING result named by recovered lock evidence."""
        run_id = str(owner.get("run_id") or "")
        current = self.get(run_id)
        if (
            not current
            or current.status != "RUNNING"
            or current.job_type != str(owner.get("job_type") or "")
            or current.started_at != str(owner.get("acquired_at") or "")
        ):
            return False
        completed = datetime.fromisoformat(detected_at)
        started = datetime.fromisoformat(current.started_at)
        warning = (
            "Observation was interrupted; its same-host process was confirmed "
            f"not running during lock recovery by {recovery_run_id}."
        )
        updated = replace(
            current,
            completed_at=detected_at,
            duration_seconds=max(0.0, (completed - started).total_seconds()),
            status="FAILED",
            warnings=current.warnings + (warning,),
            errors=current.errors + (
                "Observation process ended before releasing its repository lock.",
            ),
        )
        self.update(updated)
        return True

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".observation-", suffix=".tmp", dir=path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


class _ObservationRecoveryGuard:
    """Short OS-owned guard serializing lock acquisition and orphan recovery."""

    def __init__(self, path: Path, timeout: float = 2.0):
        self.path = Path(path).resolve()
        self.timeout = max(0.0, float(timeout))
        self._file = None

    def __enter__(self) -> "_ObservationRecoveryGuard":
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
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise ObservationOverlapError()
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

    def __exit__(self, exc_type, exc, traceback) -> None:
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


class ObservationLock:
    """Conservative repository lock with proven same-host orphan recovery."""

    def __init__(
        self,
        path: Path,
        *,
        job_type: str,
        run_id: str,
        acquired_at: str,
        orphan_evidence_root: Path | None = None,
        process_liveness: Callable[[int], bool | None] | None = None,
        current_host: str | None = None,
        detected_at: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path).resolve()
        self.orphan_evidence_root = Path(
            orphan_evidence_root
            or self.path.parent / "curation_observations" / "_orphaned_locks"
        ).resolve()
        self.guard_path = self.orphan_evidence_root / ".recovery.guard"
        self.process_liveness = process_liveness or self._process_liveness
        self.current_host = str(current_host or socket.gethostname()).strip()
        self.detected_at = detected_at or (lambda: datetime.now(timezone.utc))
        self.metadata = {
            "pid": os.getpid(),
            "host": self.current_host,
            "job_type": str(job_type),
            "run_id": str(run_id),
            "acquired_at": str(acquired_at),
        }
        self.acquired = False
        self.recovered_owner: dict[str, Any] | None = None
        self.recovery_evidence_path: Path | None = None

    def __enter__(self) -> "ObservationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _ObservationRecoveryGuard(self.guard_path):
            descriptor = self._acquire_or_recover()
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.metadata, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired and self._owner().get("run_id") == self.metadata["run_id"]:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def _owner(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _acquire_or_recover(self) -> int:
        try:
            return os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            owner = self._owner()
            if not self._proven_orphan(owner):
                raise ObservationOverlapError(owner) from error
            evidence = self._preserve_orphan(owner)
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError as overlap:
                raise ObservationOverlapError(self._owner()) from overlap
            self.recovered_owner = owner
            self.recovery_evidence_path = evidence
            return descriptor

    def _proven_orphan(self, owner: dict[str, Any]) -> bool:
        required = ("host", "pid", "job_type", "run_id", "acquired_at")
        if not isinstance(owner, dict) or any(not owner.get(key) for key in required):
            return False
        host = owner.get("host")
        pid = owner.get("pid")
        job_type = owner.get("job_type")
        run_id = owner.get("run_id")
        acquired_at = owner.get("acquired_at")
        if (
            not isinstance(host, str)
            or host.strip().casefold() != self.current_host.casefold()
            or not isinstance(job_type, str)
            or not re.fullmatch(r"[a-z0-9-]{1,64}", job_type)
            or not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", run_id)
            or not isinstance(acquired_at, str)
        ):
            return False
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            acquired = datetime.fromisoformat(acquired_at)
        except ValueError:
            return False
        if acquired.tzinfo is None:
            return False
        try:
            running = self.process_liveness(pid)
        except Exception:
            return False
        return running is False

    def _preserve_orphan(self, owner: dict[str, Any]) -> Path:
        self.orphan_evidence_root.mkdir(parents=True, exist_ok=True)
        run_id = str(owner.get("run_id") or "unknown")
        safe_run_id = "".join(
            character for character in run_id
            if character.isalnum() or character in "-_."
        )[:160] or "unknown"
        evidence = self.orphan_evidence_root / (
            f"{safe_run_id}-{uuid4().hex[:12].upper()}.lock.json"
        )
        try:
            os.replace(self.path, evidence)
        except OSError as error:
            raise ObservationOverlapError(owner) from error
        return evidence

    @staticmethod
    def _process_liveness(pid: int) -> bool | None:
        """Return True/False only when process liveness can be proven."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if handle:
                exit_code = wintypes.DWORD()
                try:
                    if not kernel32.GetExitCodeProcess(
                        handle, ctypes.byref(exit_code)
                    ):
                        return None
                    return exit_code.value == 259
                finally:
                    kernel32.CloseHandle(handle)
            error = ctypes.get_last_error()
            if error == 87:
                return False
            if error == 5:
                return True
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
