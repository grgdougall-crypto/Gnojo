from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAGE_B_STATUSES = frozenset({"PREPARED", "COMMITTED", "SKIPPED", "FAILED"})
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")


class StageBJournalError(RuntimeError):
    pass


@dataclass(frozen=True)
class StageBJournalEvent:
    schema_version: int
    event_id: str
    revision: int
    previous_event_digest: str
    event_digest: str
    run_id: str
    correlation_id: str
    capability_id: str
    capability_version: int
    task_id: str
    finding_id: str
    idempotency_key: str
    precondition_fingerprint: str
    before_task_fingerprint: str
    after_task_fingerprint: str
    declared_mutation_fields: tuple[str, ...]
    at: str
    status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "revision": self.revision,
            "previous_event_digest": self.previous_event_digest,
            "event_digest": self.event_digest,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "task_id": self.task_id,
            "finding_id": self.finding_id,
            "idempotency_key": self.idempotency_key,
            "precondition_fingerprint": self.precondition_fingerprint,
            "before_task_fingerprint": self.before_task_fingerprint,
            "after_task_fingerprint": self.after_task_fingerprint,
            "declared_mutation_fields": list(self.declared_mutation_fields),
            "at": self.at,
            "status": self.status,
            "reason": self.reason,
        }

    @classmethod
    def build(cls, *, previous: "StageBJournalEvent | None" = None, **values: Any
              ) -> "StageBJournalEvent":
        payload = {
            "schema_version": 1,
            "revision": previous.revision + 1 if previous else 1,
            "previous_event_digest": previous.event_digest if previous else "",
            **values,
            "event_digest": "",
        }
        event = cls.from_dict(payload, verify_digest=False)
        digest = cls.digest(event.to_dict())
        return cls.from_dict({**event.to_dict(), "event_digest": digest})

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, verify_digest: bool = True
    ) -> "StageBJournalEvent":
        if not isinstance(value, dict):
            raise StageBJournalError("Stage B journal event must be an object.")
        try:
            event = cls(
                schema_version=int(value.get("schema_version")),
                event_id=str(value.get("event_id") or ""),
                revision=int(value.get("revision")),
                previous_event_digest=str(value.get("previous_event_digest") or ""),
                event_digest=str(value.get("event_digest") or ""),
                run_id=str(value.get("run_id") or ""),
                correlation_id=str(value.get("correlation_id") or ""),
                capability_id=str(value.get("capability_id") or ""),
                capability_version=int(value.get("capability_version")),
                task_id=str(value.get("task_id") or ""),
                finding_id=str(value.get("finding_id") or ""),
                idempotency_key=str(value.get("idempotency_key") or ""),
                precondition_fingerprint=str(value.get("precondition_fingerprint") or ""),
                before_task_fingerprint=str(value.get("before_task_fingerprint") or ""),
                after_task_fingerprint=str(value.get("after_task_fingerprint") or ""),
                declared_mutation_fields=tuple(
                    str(item) for item in value.get("declared_mutation_fields") or ()
                ),
                at=str(value.get("at") or ""),
                status=str(value.get("status") or ""),
                reason=str(value.get("reason") or ""),
            )
        except (TypeError, ValueError) as error:
            raise StageBJournalError("Stage B journal event fields are invalid.") from error
        event._validate(verify_digest=verify_digest)
        return event

    def _validate(self, *, verify_digest: bool) -> None:
        if self.schema_version != 1 or self.revision < 1 or self.capability_version < 1:
            raise StageBJournalError("Stage B journal version or revision is invalid.")
        for name, value in (
            ("event_id", self.event_id), ("run_id", self.run_id),
            ("capability_id", self.capability_id), ("task_id", self.task_id),
            ("finding_id", self.finding_id),
        ):
            if not IDENTITY_PATTERN.fullmatch(value):
                raise StageBJournalError(f"Stage B journal {name} is invalid.")
        if self.correlation_id and not IDENTITY_PATTERN.fullmatch(self.correlation_id):
            raise StageBJournalError("Stage B journal correlation ID is invalid.")
        if not FINGERPRINT_PATTERN.fullmatch(self.idempotency_key):
            raise StageBJournalError("Stage B idempotency key is invalid.")
        for value in (
            self.precondition_fingerprint,
            self.before_task_fingerprint,
            self.after_task_fingerprint,
        ):
            if value and not FINGERPRINT_PATTERN.fullmatch(value):
                raise StageBJournalError("Stage B journal fingerprint is invalid.")
        if self.status not in STAGE_B_STATUSES or not self.at or not self.reason:
            raise StageBJournalError("Stage B journal status, time, or reason is invalid.")
        allowed = {
            "current_verification", "last_verified_fingerprint", "history",
            "current_evidence", "structured_evidence",
        }
        if not set(self.declared_mutation_fields).issubset(allowed):
            raise StageBJournalError("Stage B journal declares an unsupported mutation.")
        if self.previous_event_digest and not FINGERPRINT_PATTERN.fullmatch(
            self.previous_event_digest
        ):
            raise StageBJournalError("Stage B journal chain fingerprint is invalid.")
        if verify_digest:
            if not FINGERPRINT_PATTERN.fullmatch(self.event_digest):
                raise StageBJournalError("Stage B journal event digest is invalid.")
            if self.event_digest != self.digest(self.to_dict()):
                raise StageBJournalError("Stage B journal event digest does not match.")

    @staticmethod
    def digest(value: dict[str, Any]) -> str:
        payload = dict(value)
        payload["event_digest"] = ""
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class StageBJournalRepository:
    """Append-only, hash-chained Stage B operational reconciliation history."""

    def __init__(self, curator_root: Path):
        self.root = Path(curator_root).resolve() / "stage_b_reconciliations"

    def append(self, event: StageBJournalEvent) -> StageBJournalEvent:
        self.validate_all()
        history = self.get(event.idempotency_key)
        previous = history[-1] if history else None
        if event.revision != (previous.revision + 1 if previous else 1):
            raise StageBJournalError("Stage B journal revision is not append-only.")
        if event.previous_event_digest != (previous.event_digest if previous else ""):
            raise StageBJournalError("Stage B journal chain does not match current history.")
        directory = self.root / event.idempotency_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{event.revision:06d}-{event.event_id}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(event.to_dict(), handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise StageBJournalError("Stage B journal event already exists.") from error
        except OSError as error:
            path.unlink(missing_ok=True)
            raise StageBJournalError(f"Unable to append Stage B journal: {error}") from error
        return event

    def get(self, idempotency_key: str) -> tuple[StageBJournalEvent, ...]:
        if not FINGERPRINT_PATTERN.fullmatch(str(idempotency_key or "")):
            raise StageBJournalError("Stage B idempotency key is invalid.")
        directory = self.root / idempotency_key
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise StageBJournalError("Stage B journal entry is malformed.")
        result = []
        previous_digest = ""
        for expected_revision, path in enumerate(sorted(directory.iterdir()), start=1):
            if not path.is_file() or path.suffix != ".json":
                raise StageBJournalError("Stage B journal contains an unexpected entry.")
            try:
                event = StageBJournalEvent.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, StageBJournalError) as error:
                raise StageBJournalError(f"Stage B journal is corrupt: {error}") from error
            if (
                path.name != f"{event.revision:06d}-{event.event_id}.json"
                or event.idempotency_key != idempotency_key
                or event.revision != expected_revision
                or event.previous_event_digest != previous_digest
            ):
                raise StageBJournalError("Stage B journal identity or chain is invalid.")
            result.append(event)
            previous_digest = event.event_digest
        return tuple(result)

    def validate_all(self) -> None:
        if not self.root.exists():
            return
        if not self.root.is_dir():
            raise StageBJournalError("Stage B journal root is malformed.")
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not FINGERPRINT_PATTERN.fullmatch(directory.name):
                raise StageBJournalError("Stage B journal contains an invalid identity.")
            self.get(directory.name)

    def committed(self, idempotency_key: str) -> bool:
        return any(event.status == "COMMITTED" for event in self.get(idempotency_key))
