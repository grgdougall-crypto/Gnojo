from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


PURE_OBSERVATION = "PURE_OBSERVATION"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
SKIPPED_OVERLAP = "SKIPPED_OVERLAP"
OBSERVATION_STATUSES = frozenset({RUNNING, SUCCEEDED, FAILED, SKIPPED_OVERLAP})
UNKNOWN_TRIGGER_SOURCE = "unknown"
TRIGGER_SOURCES = frozenset({"manual", "scheduled", UNKNOWN_TRIGGER_SOURCE})


@dataclass(frozen=True)
class ObservationJobDefinition:
    job_type: str
    execution_class: str = PURE_OBSERVATION


@dataclass(frozen=True)
class ObservationPayload:
    observation_counts: tuple[tuple[str, int], ...] = ()
    summary: tuple[tuple[str, str | int | float | bool | None], ...] = ()
    warnings: tuple[str, ...] = ()
    policy_versions: tuple[str, ...] = ()
    lifecycle_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_pairs(self.observation_counts, counts=True)
        _validate_pairs(self.summary, counts=False)


@dataclass(frozen=True)
class ObservationRunResult:
    run_id: str
    job_type: str
    execution_class: str
    trigger_source: str
    scheduler_correlation_id: str
    repository_identity: str
    application_identity: str
    started_at: str
    completed_at: str
    duration_seconds: float | None
    status: str
    observation_counts: tuple[tuple[str, int], ...]
    summary: tuple[tuple[str, str | int | float | bool | None], ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    policy_versions: tuple[str, ...]
    lifecycle_versions: tuple[str, ...]
    trusted_content_changed: bool
    curator_state_changed: bool
    operational_result_written: bool

    def __post_init__(self) -> None:
        if self.execution_class != PURE_OBSERVATION:
            raise ValueError("Stage A supports PURE_OBSERVATION only.")
        if self.trigger_source not in TRIGGER_SOURCES:
            raise ValueError("Observation trigger source is invalid.")
        if self.status not in OBSERVATION_STATUSES:
            raise ValueError("Observation result status is invalid.")
        if self.trusted_content_changed or self.curator_state_changed:
            raise ValueError("Stage A observation results cannot declare state mutation.")
        _validate_pairs(self.observation_counts, counts=True)
        _validate_pairs(self.summary, counts=False)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observation_counts"] = dict(self.observation_counts)
        value["summary"] = dict(self.summary)
        value["warnings"] = list(self.warnings)
        value["errors"] = list(self.errors)
        value["policy_versions"] = list(self.policy_versions)
        value["lifecycle_versions"] = list(self.lifecycle_versions)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObservationRunResult":
        if not isinstance(value, dict):
            raise ValueError("Observation result must be an object.")
        counts = value.get("observation_counts") or {}
        summary = value.get("summary") or {}
        if not isinstance(counts, dict) or not isinstance(summary, dict):
            raise ValueError("Observation result counts and summary must be objects.")
        return cls(
            run_id=str(value.get("run_id") or ""),
            job_type=str(value.get("job_type") or ""),
            execution_class=str(value.get("execution_class") or ""),
            trigger_source=str(
                value.get("trigger_source") or UNKNOWN_TRIGGER_SOURCE
            ),
            scheduler_correlation_id=str(value.get("scheduler_correlation_id") or ""),
            repository_identity=str(value.get("repository_identity") or ""),
            application_identity=str(value.get("application_identity") or ""),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            duration_seconds=value.get("duration_seconds"),
            status=str(value.get("status") or ""),
            observation_counts=tuple(sorted(
                (str(key), int(count)) for key, count in counts.items()
            )),
            summary=tuple(sorted(
                (str(key), item) for key, item in summary.items()
            )),
            warnings=tuple(str(item) for item in value.get("warnings") or ()),
            errors=tuple(str(item) for item in value.get("errors") or ()),
            policy_versions=tuple(
                str(item) for item in value.get("policy_versions") or ()
            ),
            lifecycle_versions=tuple(
                str(item) for item in value.get("lifecycle_versions") or ()
            ),
            trusted_content_changed=bool(value.get("trusted_content_changed")),
            curator_state_changed=bool(value.get("curator_state_changed")),
            operational_result_written=bool(value.get("operational_result_written")),
        )


def _validate_pairs(values: tuple[tuple[str, Any], ...], *, counts: bool) -> None:
    if not isinstance(values, tuple):
        raise ValueError("Observation mappings must use immutable tuples.")
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str):
            raise ValueError("Observation mapping entries are invalid.")
        value = item[1]
        if counts and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Observation counts must be integers.")
        if not counts and not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("Observation summaries must contain scalar values only.")
