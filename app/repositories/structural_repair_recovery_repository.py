from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from app.services.curator_structural_repair_governance import (
    APPLICATION_ID_PATTERN,
    StructuralRepairFingerprint,
)


class StructuralRepairRecoveryRepositoryError(RuntimeError):
    """Exact-byte structural recovery provenance is unavailable or malformed."""


class StructuralRepairRecoveryRepository:
    """Append-only recovery material and compensating events for applied repairs."""

    def __init__(self, curator_root: Path):
        self.root = Path(curator_root).resolve() / "structural_repair_recoveries"

    def capture(self, *, application_id: str, approval_id: str, task_id: str,
                finding_id: str, fix_session_id: str, reviewer_identity: str,
                workflow_id: str, workflow_path: str, original_bytes: bytes,
                raw_before: str, semantic_before: str, expected_raw_after: str,
                expected_semantic_after: str, captured_at: str) -> dict[str, Any]:
        if not isinstance(original_bytes, bytes):
            raise StructuralRepairRecoveryRepositoryError("Recovery content must be exact bytes.")
        if StructuralRepairFingerprint.raw_workflow(original_bytes) != raw_before:
            raise StructuralRepairRecoveryRepositoryError(
                "Recovery bytes do not match the recorded original fingerprint."
            )
        value = {
            "schema_version": "1.0",
            "application_id": application_id,
            "approval_id": str(approval_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "finding_id": str(finding_id or "").strip(),
            "fix_session_id": str(fix_session_id or "").strip(),
            "reviewer_identity": str(reviewer_identity or "").strip(),
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": str(workflow_id or "").strip(),
            "workflow_path": str(workflow_path or "").strip().replace("\\", "/"),
            "workflow_raw_sha256_before": raw_before,
            "workflow_semantic_sha256_before": semantic_before,
            "expected_workflow_raw_sha256_after": expected_raw_after,
            "expected_workflow_semantic_sha256_after": expected_semantic_after,
            "captured_at": str(captured_at or "").strip(),
            "original_bytes_base64": base64.b64encode(original_bytes).decode("ascii"),
        }
        self._validate_material(value)
        directory = self._directory(application_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "material.json"
        if path.exists():
            existing = self.get(application_id)
            comparable = dict(existing)
            comparable.pop("original_bytes", None)
            if comparable != value:
                raise StructuralRepairRecoveryRepositoryError(
                    "Recovery material already exists with different transaction identity."
                )
            return existing
        self._write_new(path, value)
        return self.get(application_id)

    def get(self, application_id: str) -> dict[str, Any]:
        path = self._directory(application_id) / "material.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            self._validate_material(value)
            original = base64.b64decode(value["original_bytes_base64"], validate=True)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise StructuralRepairRecoveryRepositoryError(
                "Structural repair recovery material is unavailable or malformed."
            ) from error
        if StructuralRepairFingerprint.raw_workflow(original) != value["workflow_raw_sha256_before"]:
            raise StructuralRepairRecoveryRepositoryError(
                "Structural repair recovery bytes failed fingerprint verification."
            )
        result = dict(value)
        result["original_bytes"] = original
        return result

    def append_event(self, application_id: str, *, outcome: str, reviewer_identity: str,
                     fix_session_id: str, reason: str, current_raw_sha256: str,
                     current_semantic_sha256: str, restored_raw_sha256: str = "",
                     restored_semantic_sha256: str = "", occurred_at: str) -> dict[str, Any]:
        if outcome not in {"pending", "recovered", "failed"}:
            raise StructuralRepairRecoveryRepositoryError("Recovery outcome is unsupported.")
        bounded_reason = str(reason or "").strip()
        if not bounded_reason or len(bounded_reason) > 1000:
            raise StructuralRepairRecoveryRepositoryError(
                "A bounded operator recovery reason is required."
            )
        material = self.get(application_id)
        events = self.events(application_id)
        revision = len(events) + 1
        previous_digest = events[-1]["event_digest"] if events else ""
        event = {
            "schema_version": "1.0",
            "recovery_event_id": "SRR-" + secrets.token_hex(8).upper(),
            "application_id": application_id,
            "approval_id": material["approval_id"],
            "task_id": material["task_id"],
            "finding_id": material["finding_id"],
            "workflow_id": material["workflow_id"],
            "workflow_path": material["workflow_path"],
            "revision": revision,
            "previous_event_digest": previous_digest,
            "reviewer_identity": str(reviewer_identity or "").strip(),
            "reviewer_identity_assurance": "application_supplied",
            "fix_session_id": str(fix_session_id or "").strip(),
            "reason": bounded_reason,
            "current_raw_sha256": current_raw_sha256,
            "current_semantic_sha256": current_semantic_sha256,
            "restored_raw_sha256": restored_raw_sha256,
            "restored_semantic_sha256": restored_semantic_sha256,
            "outcome": outcome,
            "occurred_at": str(occurred_at or "").strip(),
        }
        event["event_digest"] = StructuralRepairFingerprint.contract(event)
        path = self._directory(application_id) / (
            f"{revision:06d}-{event['recovery_event_id']}.json"
        )
        self._write_new(path, event)
        return event

    def events(self, application_id: str) -> tuple[dict[str, Any], ...]:
        directory = self._directory(application_id)
        if not directory.exists():
            return ()
        events = []
        previous_digest = ""
        for path in sorted(directory.glob("*-SRR-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise StructuralRepairRecoveryRepositoryError(
                    "Structural repair recovery history is malformed."
                ) from error
            if (value.get("application_id") != application_id
                    or value.get("revision") != len(events) + 1
                    or value.get("previous_event_digest", "") != previous_digest):
                raise StructuralRepairRecoveryRepositoryError(
                    "Structural repair recovery history chain is invalid."
                )
            digest_payload = dict(value)
            recorded_digest = digest_payload.pop("event_digest", "")
            if StructuralRepairFingerprint.contract(digest_payload) != recorded_digest:
                raise StructuralRepairRecoveryRepositoryError(
                    "Structural repair recovery event fingerprint is invalid."
                )
            events.append(value)
            previous_digest = recorded_digest
        return tuple(events)

    @staticmethod
    def _validate_material(value: dict[str, Any]) -> None:
        required = {
            "application_id", "approval_id", "task_id", "finding_id", "fix_session_id",
            "reviewer_identity", "workflow_id", "workflow_path",
            "workflow_raw_sha256_before", "workflow_semantic_sha256_before",
            "expected_workflow_raw_sha256_after",
            "expected_workflow_semantic_sha256_after", "captured_at",
            "original_bytes_base64",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise StructuralRepairRecoveryRepositoryError("Recovery material is incomplete.")
        if not APPLICATION_ID_PATTERN.fullmatch(str(value.get("application_id") or "")):
            raise StructuralRepairRecoveryRepositoryError("Recovery application ID is invalid.")
        if any(not str(value.get(field) or "").strip() for field in required):
            raise StructuralRepairRecoveryRepositoryError("Recovery material contains empty identity fields.")
        if value.get("reviewer_identity_assurance") != "application_supplied":
            raise StructuralRepairRecoveryRepositoryError("Recovery reviewer assurance is invalid.")
        path = str(value.get("workflow_path") or "").replace("\\", "/")
        if not path.startswith("app/workflow_drafts/") or Path(path).suffix != ".json":
            raise StructuralRepairRecoveryRepositoryError("Recovery workflow path is invalid.")

    def _directory(self, application_id: str) -> Path:
        value = str(application_id or "").strip()
        if not APPLICATION_ID_PATTERN.fullmatch(value):
            raise StructuralRepairRecoveryRepositoryError("Application ID is invalid.")
        return self.root / value

    @staticmethod
    def _write_new(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError as error:
            raise StructuralRepairRecoveryRepositoryError(
                "Structural repair recovery provenance is append-only."
            ) from error
        except OSError as error:
            path.unlink(missing_ok=True)
            raise StructuralRepairRecoveryRepositoryError(
                f"Unable to persist structural repair recovery provenance: {error}"
            ) from error
