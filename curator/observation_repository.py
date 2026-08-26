from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

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


class ObservationLock:
    """One conservative repository-wide lock; stale locks require manual recovery."""

    def __init__(self, path: Path, *, job_type: str, run_id: str, acquired_at: str):
        self.path = Path(path).resolve()
        self.metadata = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "job_type": str(job_type),
            "run_id": str(run_id),
            "acquired_at": str(acquired_at),
        }
        self.acquired = False

    def __enter__(self) -> "ObservationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise ObservationOverlapError(self._owner()) from error
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
