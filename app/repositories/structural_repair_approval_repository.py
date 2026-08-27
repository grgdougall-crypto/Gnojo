from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.services.curator_structural_repair_governance import (
    APPROVAL_ID_PATTERN,
    StructuralRepairApproval,
    StructuralRepairFingerprint,
    StructuralRepairGovernanceError,
)


class StructuralRepairApprovalRepositoryError(RuntimeError):
    """A server-owned structural approval could not be read or advanced safely."""


class StructuralRepairApprovalRepository:
    STATES = frozenset({"approved", "consumed", "expired", "invalidated"})

    def __init__(self, curator_root: Path):
        self.root = Path(curator_root).resolve() / "structural_repair_approvals"

    def issue(self, approval: StructuralRepairApproval, preview: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(approval, StructuralRepairApproval):
            raise StructuralRepairApprovalRepositoryError("A validated approval artifact is required.")
        if StructuralRepairFingerprint.contract(preview) != approval.preview_digest:
            raise StructuralRepairApprovalRepositoryError("Stored preview does not match the approval digest.")
        directory = self._directory(approval.approval_id)
        directory.mkdir(parents=True, exist_ok=False)
        self._write_new(directory / "approval.json", {
            "approval": approval.to_dict(), "preview": preview,
        })
        self._write_new(directory / "000001-approved.json", {
            "revision": 1, "state": "approved", "reason": "issued",
        })
        return self.get(approval.approval_id)

    def get(self, approval_id: str) -> dict[str, Any]:
        directory = self._directory(approval_id)
        path = directory / "approval.json"
        if not path.is_file():
            raise StructuralRepairApprovalRepositoryError("Structural repair approval was not found.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            approval = StructuralRepairApproval.from_dict(raw["approval"])
            preview = raw["preview"]
            if not isinstance(preview, dict):
                raise ValueError("preview must be an object")
            if StructuralRepairFingerprint.contract(preview) != approval.preview_digest:
                raise ValueError("preview digest mismatch")
            events = self._events(directory)
        except (OSError, KeyError, ValueError, json.JSONDecodeError,
                StructuralRepairGovernanceError) as error:
            raise StructuralRepairApprovalRepositoryError(
                f"Structural repair approval is malformed: {error}"
            ) from error
        return {"approval": approval, "preview": preview, "state": events[-1]["state"],
                "events": tuple(events)}

    def transition(self, approval_id: str, state: str, reason: str) -> dict[str, Any]:
        value = self.get(approval_id)
        state = str(state or "").strip()
        if state not in self.STATES - {"approved"}:
            raise StructuralRepairApprovalRepositoryError("Approval transition is unsupported.")
        if value["state"] != "approved":
            if value["state"] == state:
                return value
            raise StructuralRepairApprovalRepositoryError("Approval is no longer applicable.")
        revision = len(value["events"]) + 1
        self._write_new(self._directory(approval_id) / f"{revision:06d}-{state}.json", {
            "revision": revision, "state": state, "reason": str(reason or "")[:500],
        })
        return self.get(approval_id)

    def list_approval_ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        values = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or not APPROVAL_ID_PATTERN.fullmatch(path.name):
                raise StructuralRepairApprovalRepositoryError(
                    "Structural repair approval repository contains an invalid entry."
                )
            values.append(path.name)
        return tuple(values)

    def _events(self, directory: Path) -> list[dict[str, Any]]:
        events = []
        for path in sorted(directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9]-*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            revision, state = raw.get("revision"), raw.get("state")
            if revision != len(events) + 1 or state not in self.STATES:
                raise ValueError("approval state history is invalid")
            events.append({"revision": revision, "state": state,
                           "reason": str(raw.get("reason") or "")})
        if not events or events[0]["state"] != "approved":
            raise ValueError("approval state history is incomplete")
        return events

    def _directory(self, approval_id: str) -> Path:
        value = str(approval_id or "").strip()
        if not APPROVAL_ID_PATTERN.fullmatch(value):
            raise StructuralRepairApprovalRepositoryError("Approval ID is invalid.")
        return self.root / value

    @staticmethod
    def _write_new(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError as error:
            raise StructuralRepairApprovalRepositoryError(
                "Structural repair approval history is immutable."
            ) from error
