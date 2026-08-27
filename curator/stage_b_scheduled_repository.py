from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


RUN_STATUSES = frozenset({
    "RUNNING", "SUCCEEDED", "SUCCEEDED_NO_CHANGES", "FAILED", "PARTIAL_FAILED",
})
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


class StageBScheduledRunRepositoryError(RuntimeError):
    pass


class StageBScheduledRunRepository:
    """Atomic operational records for explicit scheduled Stage B runs."""

    def __init__(self, curator_root: Path):
        self.root = Path(curator_root).resolve() / "stage_b_scheduled_runs"

    def create_running(self, value: dict[str, Any]) -> dict[str, Any]:
        record = self._validated(value, expected_status="RUNNING")
        path = self._path(record["runner_id"])
        if path.exists():
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner identity already exists."
            )
        self._write(path, record, replace=False)
        return deepcopy(record)

    def finalize(self, runner_id: str, value: dict[str, Any]) -> dict[str, Any]:
        path = self._path(runner_id)
        current = self.get(runner_id)
        if current is None or current["status"] != "RUNNING":
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner is not in a finalizable RUNNING state."
            )
        record = self._validated(value)
        if record["runner_id"] != runner_id or record["status"] == "RUNNING":
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner final state is invalid."
            )
        self._write(path, record, replace=True)
        return deepcopy(record)

    def get(self, runner_id: str) -> dict[str, Any] | None:
        path = self._path(runner_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StageBScheduledRunRepositoryError(
                f"Stage B scheduled runner record is unreadable: {error}"
            ) from error
        return deepcopy(self._validated(value))

    def latest(self) -> dict[str, Any] | None:
        if not self.root.exists():
            return None
        if not self.root.is_dir():
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner repository is malformed."
            )
        records = []
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.suffix != ".json":
                raise StageBScheduledRunRepositoryError(
                    "Stage B scheduled runner repository contains an unexpected entry."
                )
            record = self.get(path.stem)
            if record is None or path.name != f"{record['runner_id']}.json":
                raise StageBScheduledRunRepositoryError(
                    "Stage B scheduled runner record identity is inconsistent."
                )
            records.append(record)
        return deepcopy(max(
            records,
            key=lambda item: (item["started_at"], item["runner_id"]),
            default=None,
        ))

    def _path(self, runner_id: str) -> Path:
        if not IDENTITY_PATTERN.fullmatch(str(runner_id or "")):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner identity is invalid."
            )
        return self.root / f"{runner_id}.json"

    @staticmethod
    def _validated(
        value: dict[str, Any], *, expected_status: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner record must be an object."
            )
        record = deepcopy(value)
        required = {
            "schema_version", "runner_id", "correlation_id", "trigger_source",
            "started_at", "completed_at", "status", "allowlisted_capabilities",
            "discovered_count", "preflight_skipped_count",
            "committed_no_op_count", "committed_count", "runtime_skipped_count",
            "failed_count", "per_capability_counts", "last_processed_task",
            "failure_reason",
        }
        if set(record) != required or record.get("schema_version") != 1:
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner record schema is invalid."
            )
        if not IDENTITY_PATTERN.fullmatch(str(record.get("runner_id") or "")):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner identity is invalid."
            )
        correlation = str(record.get("correlation_id") or "")
        if correlation and not IDENTITY_PATTERN.fullmatch(correlation):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled correlation identity is invalid."
            )
        status = str(record.get("status") or "")
        if status not in RUN_STATUSES or (expected_status and status != expected_status):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner status is invalid."
            )
        if record.get("trigger_source") != "scheduled" or not record.get("started_at"):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled runner provenance is invalid."
            )
        if status == "RUNNING" and record.get("completed_at"):
            raise StageBScheduledRunRepositoryError(
                "A RUNNING Stage B runner cannot have a completion time."
            )
        if status != "RUNNING" and not record.get("completed_at"):
            raise StageBScheduledRunRepositoryError(
                "A completed Stage B runner requires a completion time."
            )
        for name in (
            "discovered_count", "preflight_skipped_count", "committed_no_op_count",
            "committed_count", "runtime_skipped_count", "failed_count",
        ):
            if not isinstance(record.get(name), int) or record[name] < 0:
                raise StageBScheduledRunRepositoryError(
                    "Stage B scheduled runner counts are invalid."
                )
        if not isinstance(record.get("allowlisted_capabilities"), list):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled allowlist is invalid."
            )
        if not isinstance(record.get("per_capability_counts"), dict):
            raise StageBScheduledRunRepositoryError(
                "Stage B scheduled capability counts are invalid."
            )
        return record

    def _write(self, path: Path, value: dict[str, Any], *, replace: bool) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError as error:
                    raise StageBScheduledRunRepositoryError(
                        "Stage B scheduled runner identity already exists."
                    ) from error
        except OSError as error:
            raise StageBScheduledRunRepositoryError(
                f"Unable to persist Stage B scheduled runner result: {error}"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
