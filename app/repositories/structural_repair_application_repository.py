from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.services.curator_structural_repair_governance import (
    APPLICATION_ID_PATTERN,
    StructuralRepairApplicationRecord,
    StructuralRepairGovernanceError,
)


class StructuralRepairApplicationRepositoryError(RuntimeError):
    """Structural repair application history could not be read or appended safely."""


class StructuralRepairApplicationRepository:
    """Append-only provenance journal. This repository has no workflow-write authority."""

    IMMUTABLE_TRANSACTION_FIELDS = (
        "schema_version", "application_id", "approval_id", "task_id", "finding_id", "fix_session_id",
        "reviewer_identity", "reviewer_identity_assurance", "workflow_id", "workflow_path",
        "workflow_raw_sha256_before", "workflow_semantic_sha256_before",
        "expected_workflow_raw_sha256_after", "expected_workflow_semantic_sha256_after",
        "preview_digest", "plan_digest", "adapter_id", "specification_id",
        "specification_version", "specification_digest", "proposed_node_ids",
        "changed_edges", "new_edges", "metadata_changes", "created_at",
    )

    def __init__(self, curator_root: Path):
        self.root = curator_root.resolve() / "structural_repair_applications"

    def append(self, record: StructuralRepairApplicationRecord | dict[str, Any]) -> dict[str, Any]:
        try:
            value = record if isinstance(record, StructuralRepairApplicationRecord) else (
                StructuralRepairApplicationRecord.from_dict(record)
            )
        except StructuralRepairGovernanceError as error:
            raise StructuralRepairApplicationRepositoryError(str(error)) from error
        history = self.get(value.application_id)
        if history:
            previous = history[-1]
            if value.revision != previous.revision + 1:
                raise StructuralRepairApplicationRepositoryError(
                    "Application revision must append exactly after current history."
                )
            if value.previous_event_digest != previous.event_digest:
                raise StructuralRepairApplicationRepositoryError(
                    "Application revision does not reference the current journal event."
                )
            changed_identity = [field for field in self.IMMUTABLE_TRANSACTION_FIELDS
                                if getattr(value, field) != getattr(previous, field)]
            if changed_identity:
                raise StructuralRepairApplicationRepositoryError(
                    "Application identity cannot change between journal revisions: "
                    + ", ".join(changed_identity)
                )
        elif value.revision != 1:
            raise StructuralRepairApplicationRepositoryError(
                "A new application journal must begin at revision 1."
            )

        directory = self._directory(value.application_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{value.revision:06d}-{value.event_id}.json"
        payload = json.dumps(value.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError as error:
            raise StructuralRepairApplicationRepositoryError(
                "Application history is append-only and this event already exists."
            ) from error
        except OSError as error:
            path.unlink(missing_ok=True)
            raise StructuralRepairApplicationRepositoryError(
                f"Unable to append structural repair history: {error}"
            ) from error
        return value.to_dict()

    def get(self, application_id: str) -> tuple[StructuralRepairApplicationRecord, ...]:
        directory = self._directory(application_id)
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise StructuralRepairApplicationRepositoryError(
                "Structural repair application history path is malformed."
            )
        records = []
        expected_revision = 1
        previous_digest = ""
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix != ".json":
                raise StructuralRepairApplicationRepositoryError(
                    "Structural repair application history contains an unexpected entry."
                )
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = StructuralRepairApplicationRecord.from_dict(raw)
            except (OSError, json.JSONDecodeError, StructuralRepairGovernanceError) as error:
                raise StructuralRepairApplicationRepositoryError(
                    f"Structural repair application history is malformed: {error}"
                ) from error
            expected_name = f"{record.revision:06d}-{record.event_id}.json"
            if path.name != expected_name or record.application_id != application_id:
                raise StructuralRepairApplicationRepositoryError(
                    "Structural repair application history identity is inconsistent."
                )
            if record.revision != expected_revision or record.previous_event_digest != previous_digest:
                raise StructuralRepairApplicationRepositoryError(
                    "Structural repair application journal chain is invalid."
                )
            records.append(record)
            previous_digest = record.event_digest
            expected_revision += 1
        return tuple(records)

    def list_application_ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        values = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or not APPLICATION_ID_PATTERN.fullmatch(path.name):
                raise StructuralRepairApplicationRepositoryError(
                    "Structural repair application history contains an invalid application entry."
                )
            values.append(path.name)
        return tuple(values)

    def _directory(self, application_id: str) -> Path:
        value = str(application_id or "").strip()
        if not APPLICATION_ID_PATTERN.fullmatch(value):
            raise StructuralRepairApplicationRepositoryError("Application ID is invalid.")
        return self.root / value
