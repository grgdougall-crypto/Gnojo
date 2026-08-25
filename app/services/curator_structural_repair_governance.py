from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.curator_structural_repair_contracts import (
    ImmutableMapping,
    RouteEdge,
    to_plain_data,
)


STAGE3_SCHEMA_VERSION = "3.0"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_ID_PATTERN = re.compile(r"^SRA-[0-9A-F]{16}$")
APPLICATION_ID_PATTERN = re.compile(r"^SRX-[0-9A-F]{16}$")
EVENT_ID_PATTERN = re.compile(r"^SRE-[0-9A-F]{16}$")

STRUCTURAL_REPAIR_FAILURE_CATEGORIES = frozenset({
    "approval_missing",
    "approval_invalid",
    "approval_expired",
    "preview_unknown",
    "plan_invalid",
    "stale_workflow",
    "lock_unavailable",
    "validation_failed_prewrite",
    "persistence_failed",
    "validation_failed_postwrite",
    "rollback_succeeded",
    "rollback_failed",
    "already_applied",
})

APPLICATION_OUTCOMES = frozenset({"pending", "applied", "failed", "rolled_back", "already_applied"})
ROLLBACK_STATUSES = frozenset({"not_required", "pending", "succeeded", "failed"})


class StructuralRepairGovernanceError(ValueError):
    """A Stage 3 governance artifact is malformed or unsupported."""


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise StructuralRepairGovernanceError(f"{label} is required.")
    return result


def _identifier(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    result = _text(value, label)
    if not pattern.fullmatch(result):
        raise StructuralRepairGovernanceError(f"{label} is invalid.")
    return result


def _digest(value: Any, label: str, *, optional: bool = False) -> str:
    result = str(value or "").strip().lower()
    if optional and not result:
        return ""
    if not SHA256_PATTERN.fullmatch(result):
        raise StructuralRepairGovernanceError(f"{label} must be a SHA-256 digest.")
    return result


def _timestamp(value: Any, label: str, *, optional: bool = False) -> str:
    result = str(value or "").strip()
    if optional and not result:
        return ""
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise StructuralRepairGovernanceError(f"{label} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StructuralRepairGovernanceError(f"{label} must include a timezone.")
    return result


def _schema(value: Any) -> str:
    result = _text(value, "Schema version")
    if result != STAGE3_SCHEMA_VERSION:
        raise StructuralRepairGovernanceError("Structural repair governance schema is unsupported.")
    return result


def _relative_path(value: Any, label: str) -> str:
    result = _text(value, label).replace("\\", "/")
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise StructuralRepairGovernanceError(f"{label} must be a safe relative path.")
    if len(path.parts) != 3 or path.parts[:2] != ("app", "workflow_drafts") or path.suffix != ".json":
        raise StructuralRepairGovernanceError(
            f"{label} must identify one editable workflow draft."
        )
    return result


def _filename(value: Any) -> str:
    result = _text(value, "Workflow filename")
    if Path(result).name != result or not result.endswith(".json"):
        raise StructuralRepairGovernanceError("Workflow filename is invalid.")
    return result


def _immutable_data(value: Any) -> Any:
    canonical = _canonical_data(value)
    if isinstance(canonical, dict):
        return ImmutableMapping({key: _immutable_data(item) for key, item in canonical.items()})
    if isinstance(canonical, list):
        return tuple(_immutable_data(item) for item in canonical)
    return canonical


def _canonical_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    value = to_plain_data(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuralRepairGovernanceError("Canonical data cannot contain non-finite numbers.")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StructuralRepairGovernanceError("Canonical object keys must be strings.")
            result[key] = _canonical_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_data(item) for item in value]
    raise StructuralRepairGovernanceError(
        f"Unsupported canonical data type: {type(value).__name__}."
    )


class StructuralRepairFingerprint:
    """One deterministic fingerprint format for Stage 3 governance artifacts."""

    @staticmethod
    def raw_workflow(content: bytes) -> str:
        if not isinstance(content, bytes):
            raise StructuralRepairGovernanceError("Raw workflow fingerprinting requires bytes.")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def canonical_json(value: Any) -> bytes:
        return json.dumps(
            _canonical_data(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def semantic_workflow(cls, workflow: Any) -> str:
        if not isinstance(workflow, Mapping):
            raise StructuralRepairGovernanceError(
                "Semantic workflow fingerprinting requires a JSON object."
            )
        return hashlib.sha256(cls.canonical_json(workflow)).hexdigest()

    @classmethod
    def contract(cls, value: Any) -> str:
        return hashlib.sha256(cls.canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class StructuralRepairApproval:
    schema_version: str
    approval_id: str
    application_id: str
    task_id: str
    finding_id: str
    fix_session_id: str
    reviewer_identity: str
    reviewer_identity_assurance: str
    workflow_id: str
    workflow_filename: str
    workflow_lifecycle: str
    workflow_path: str
    workflow_raw_sha256_before: str
    workflow_semantic_sha256_before: str
    adapter_id: str
    plan_id: str
    plan_digest: str
    specification_id: str
    specification_version: int
    specification_digest: str
    preview_digest: str
    created_at: str
    expires_at: str
    approval_state: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StructuralRepairApproval":
        if not isinstance(value, dict):
            raise StructuralRepairGovernanceError("Structural repair approval must be an object.")
        created = _timestamp(value.get("created_at"), "Approval creation timestamp")
        expires = _timestamp(value.get("expires_at"), "Approval expiration timestamp")
        if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.fromisoformat(
                created.replace("Z", "+00:00")):
            raise StructuralRepairGovernanceError("Approval expiration must follow its creation.")
        version = value.get("specification_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StructuralRepairGovernanceError("Specification version must be a positive integer.")
        lifecycle = _text(value.get("workflow_lifecycle"), "Workflow lifecycle")
        if lifecycle != "draft":
            raise StructuralRepairGovernanceError("Structural approval may target only an editable draft.")
        assurance = _text(value.get("reviewer_identity_assurance"), "Reviewer identity assurance")
        if assurance != "application_supplied":
            raise StructuralRepairGovernanceError(
                "Reviewer identity must be labeled as application-supplied."
            )
        state = _text(value.get("approval_state"), "Approval state")
        if state != "approved":
            raise StructuralRepairGovernanceError("Stage 3.0 supports only explicit approved artifacts.")
        return cls(
            _schema(value.get("schema_version")),
            _identifier(value.get("approval_id"), "Approval ID", APPROVAL_ID_PATTERN),
            _identifier(value.get("application_id"), "Application ID", APPLICATION_ID_PATTERN),
            _text(value.get("task_id"), "Task ID"),
            str(value.get("finding_id") or "").strip(),
            _text(value.get("fix_session_id"), "Fix session ID"),
            _text(value.get("reviewer_identity"), "Reviewer identity"),
            assurance,
            _text(value.get("workflow_id"), "Workflow ID"),
            _filename(value.get("workflow_filename")),
            lifecycle,
            _relative_path(value.get("workflow_path"), "Workflow path"),
            _digest(value.get("workflow_raw_sha256_before"), "Raw workflow fingerprint"),
            _digest(value.get("workflow_semantic_sha256_before"), "Semantic workflow fingerprint"),
            _text(value.get("adapter_id"), "Adapter ID"),
            _text(value.get("plan_id"), "Plan ID"),
            _digest(value.get("plan_digest"), "Plan digest"),
            _text(value.get("specification_id"), "Specification ID"),
            version,
            _digest(value.get("specification_digest"), "Specification digest"),
            _digest(value.get("preview_digest"), "Preview digest"),
            created,
            expires,
            state,
        )

    def to_dict(self) -> dict[str, Any]:
        return _canonical_data(self)

    @property
    def identity_digest(self) -> str:
        return StructuralRepairFingerprint.contract(self)


@dataclass(frozen=True)
class StructuralRepairApplicationRecord:
    schema_version: str
    application_id: str
    approval_id: str
    event_id: str
    revision: int
    previous_event_digest: str
    task_id: str
    finding_id: str
    fix_session_id: str
    reviewer_identity: str
    reviewer_identity_assurance: str
    workflow_id: str
    workflow_path: str
    workflow_raw_sha256_before: str
    workflow_semantic_sha256_before: str
    expected_workflow_raw_sha256_after: str
    expected_workflow_semantic_sha256_after: str
    preview_digest: str
    plan_digest: str
    adapter_id: str
    specification_id: str
    specification_version: int
    specification_digest: str
    proposed_node_ids: tuple[str, ...]
    changed_edges: tuple[RouteEdge, ...]
    new_edges: tuple[RouteEdge, ...]
    created_at: str
    applied_at: str
    finalized_at: str
    validation_summaries: ImmutableMapping
    outcome: str
    failure_category: str
    failure_reason: str
    rollback_status: str
    rollback_raw_sha256: str
    rollback_semantic_sha256: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StructuralRepairApplicationRecord":
        if not isinstance(value, dict):
            raise StructuralRepairGovernanceError("Structural repair application record must be an object.")
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise StructuralRepairGovernanceError("Application revision must be a positive integer.")
        version = value.get("specification_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StructuralRepairGovernanceError("Specification version must be a positive integer.")
        previous = _digest(value.get("previous_event_digest"), "Previous event digest", optional=True)
        if revision == 1 and previous:
            raise StructuralRepairGovernanceError("The first application revision cannot have a predecessor.")
        if revision > 1 and not previous:
            raise StructuralRepairGovernanceError("Later application revisions require a predecessor digest.")
        outcome = _text(value.get("outcome"), "Application outcome")
        if outcome not in APPLICATION_OUTCOMES:
            raise StructuralRepairGovernanceError("Application outcome is unsupported.")
        failure = str(value.get("failure_category") or "").strip()
        if failure and failure not in STRUCTURAL_REPAIR_FAILURE_CATEGORIES:
            raise StructuralRepairGovernanceError("Structural repair failure category is unsupported.")
        if outcome == "failed" and not failure:
            raise StructuralRepairGovernanceError("Failed application records require a failure category.")
        reason = str(value.get("failure_reason") or "").strip()
        if len(reason) > 1000:
            raise StructuralRepairGovernanceError("Failure reason exceeds the bounded length.")
        rollback = _text(value.get("rollback_status"), "Rollback status")
        if rollback not in ROLLBACK_STATUSES:
            raise StructuralRepairGovernanceError("Rollback status is unsupported.")
        nodes = value.get("proposed_node_ids")
        if not isinstance(nodes, list) or not nodes or not all(str(item).strip() for item in nodes):
            raise StructuralRepairGovernanceError("Proposed node IDs must be an explicit nonempty list.")
        changed = value.get("changed_edges")
        new = value.get("new_edges")
        if not isinstance(changed, list) or not changed or not isinstance(new, list) or not new:
            raise StructuralRepairGovernanceError("Changed and new edge sets must be explicit and nonempty.")
        summaries = value.get("validation_summaries", {})
        if not isinstance(summaries, dict):
            raise StructuralRepairGovernanceError("Validation summaries must be an object.")
        assurance = _text(value.get("reviewer_identity_assurance"), "Reviewer identity assurance")
        if assurance != "application_supplied":
            raise StructuralRepairGovernanceError(
                "Reviewer identity must be labeled as application-supplied."
            )
        created = _timestamp(value.get("created_at"), "Application creation timestamp")
        applied = _timestamp(value.get("applied_at"), "Application applied timestamp", optional=True)
        finalized = _timestamp(value.get("finalized_at"), "Application finalized timestamp", optional=True)
        created_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if applied and datetime.fromisoformat(applied.replace("Z", "+00:00")) < created_time:
            raise StructuralRepairGovernanceError("Application timestamp cannot precede creation.")
        if finalized:
            lower_bound = datetime.fromisoformat((applied or created).replace("Z", "+00:00"))
            if datetime.fromisoformat(finalized.replace("Z", "+00:00")) < lower_bound:
                raise StructuralRepairGovernanceError("Finalization timestamp is out of order.")
        return cls(
            _schema(value.get("schema_version")),
            _identifier(value.get("application_id"), "Application ID", APPLICATION_ID_PATTERN),
            _identifier(value.get("approval_id"), "Approval ID", APPROVAL_ID_PATTERN),
            _identifier(value.get("event_id"), "Event ID", EVENT_ID_PATTERN),
            revision,
            previous,
            _text(value.get("task_id"), "Task ID"),
            str(value.get("finding_id") or "").strip(),
            _text(value.get("fix_session_id"), "Fix session ID"),
            _text(value.get("reviewer_identity"), "Reviewer identity"),
            assurance,
            _text(value.get("workflow_id"), "Workflow ID"),
            _relative_path(value.get("workflow_path"), "Workflow path"),
            _digest(value.get("workflow_raw_sha256_before"), "Raw workflow fingerprint"),
            _digest(value.get("workflow_semantic_sha256_before"), "Semantic workflow fingerprint"),
            _digest(value.get("expected_workflow_raw_sha256_after"), "Expected raw after fingerprint", optional=True),
            _digest(value.get("expected_workflow_semantic_sha256_after"), "Expected semantic after fingerprint", optional=True),
            _digest(value.get("preview_digest"), "Preview digest"),
            _digest(value.get("plan_digest"), "Plan digest"),
            _text(value.get("adapter_id"), "Adapter ID"),
            _text(value.get("specification_id"), "Specification ID"),
            version,
            _digest(value.get("specification_digest"), "Specification digest"),
            tuple(str(item).strip() for item in nodes),
            tuple(RouteEdge.from_dict(item) for item in changed),
            tuple(RouteEdge.from_dict(item) for item in new),
            created,
            applied,
            finalized,
            _immutable_data(summaries),
            outcome,
            failure,
            reason,
            rollback,
            _digest(value.get("rollback_raw_sha256"), "Rollback raw fingerprint", optional=True),
            _digest(value.get("rollback_semantic_sha256"), "Rollback semantic fingerprint", optional=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return _canonical_data(self)

    @property
    def event_digest(self) -> str:
        return StructuralRepairFingerprint.contract(self)
