from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ACCEPT_FOR_PUBLICATION = "ACCEPT_FOR_PUBLICATION"
_WORKFLOW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_REVIEW_ID = re.compile(r"WPR-[0-9A-F]{16}")
_FINDING_ID = re.compile(r"CUR-[0-9A-F]{12}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class WorkflowPublicationReviewRepositoryError(RuntimeError):
    """A workflow publication-review record is unavailable or malformed."""


@dataclass(frozen=True)
class WorkflowPublicationReasoningReview:
    schema_version: str
    review_id: str
    workflow_id: str
    draft_semantic_fingerprint: str
    finding_id: str
    rule: str
    finding_type: str
    content_identifier: str
    node_id: str
    reviewer: str
    reviewed_at: str
    disposition: str
    note: str

    @classmethod
    def create(
        cls, *, workflow_id: str, draft_semantic_fingerprint: str,
        finding_id: str, rule: str, finding_type: str,
        content_identifier: str, node_id: str, reviewer: str,
        reviewed_at: str, note: str,
    ) -> "WorkflowPublicationReasoningReview":
        return cls.from_dict({
            "schema_version": "1.0",
            "review_id": f"WPR-{uuid4().hex[:16].upper()}",
            "workflow_id": workflow_id,
            "draft_semantic_fingerprint": draft_semantic_fingerprint,
            "finding_id": finding_id,
            "rule": rule,
            "finding_type": finding_type,
            "content_identifier": content_identifier,
            "node_id": node_id,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "disposition": ACCEPT_FOR_PUBLICATION,
            "note": note,
        })

    @classmethod
    def from_dict(cls, value: Any) -> "WorkflowPublicationReasoningReview":
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "review_id", "workflow_id",
            "draft_semantic_fingerprint", "finding_id", "rule",
            "finding_type", "content_identifier", "node_id", "reviewer",
            "reviewed_at", "disposition", "note",
        }:
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review record fields are invalid."
            )
        text = {key: str(item or "").strip() for key, item in value.items()}
        if text["schema_version"] != "1.0":
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review schema version is unsupported."
            )
        if not _REVIEW_ID.fullmatch(text["review_id"]):
            raise WorkflowPublicationReviewRepositoryError("Publication-review ID is invalid.")
        if not _WORKFLOW_ID.fullmatch(text["workflow_id"]):
            raise WorkflowPublicationReviewRepositoryError("Workflow identity is invalid.")
        if not _FINGERPRINT.fullmatch(text["draft_semantic_fingerprint"]):
            raise WorkflowPublicationReviewRepositoryError("Draft fingerprint is invalid.")
        if not _FINDING_ID.fullmatch(text["finding_id"]):
            raise WorkflowPublicationReviewRepositoryError("Finding identity is invalid.")
        if not text["rule"].startswith("CUR-WR-") or len(text["rule"]) > 128:
            raise WorkflowPublicationReviewRepositoryError("Reasoning rule is invalid.")
        if not _TOKEN.fullmatch(text["finding_type"]):
            raise WorkflowPublicationReviewRepositoryError("Finding type is invalid.")
        expected_identifier = (
            f"{text['workflow_id']}:{text['node_id']}"
            if text["node_id"] else text["workflow_id"]
        )
        if (text["content_identifier"] != expected_identifier
                or (text["node_id"] and not _TOKEN.fullmatch(text["node_id"]))):
            raise WorkflowPublicationReviewRepositoryError(
                "Affected content identity is invalid."
            )
        if not text["reviewer"] or len(text["reviewer"]) > 200:
            raise WorkflowPublicationReviewRepositoryError("Reviewer identity is required.")
        if not text["note"] or len(text["note"]) > 2000:
            raise WorkflowPublicationReviewRepositoryError("A bounded review note is required.")
        try:
            reviewed_at = datetime.fromisoformat(text["reviewed_at"])
        except ValueError as error:
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review timestamp is invalid."
            ) from error
        if reviewed_at.tzinfo is None:
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review timestamp must include a timezone."
            )
        if text["disposition"] != ACCEPT_FOR_PUBLICATION:
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review disposition is unsupported."
            )
        return cls(**text)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class WorkflowPublicationReviewRepository:
    """Immutable, workflow-scoped publication reasoning-review records."""

    def __init__(self, curator_root: Path):
        self.root = Path(curator_root).resolve() / "workflow_publication_reviews"

    def add(
        self, review: WorkflowPublicationReasoningReview,
    ) -> WorkflowPublicationReasoningReview:
        if not isinstance(review, WorkflowPublicationReasoningReview):
            raise WorkflowPublicationReviewRepositoryError(
                "A validated publication-review record is required."
            )
        directory = self._directory(review.workflow_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{review.review_id}.json"
        payload = json.dumps(
            review.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        ) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise WorkflowPublicationReviewRepositoryError(
                "Publication-review records are immutable."
            ) from error
        return review

    def list_for_workflow(
        self, workflow_id: str,
    ) -> tuple[WorkflowPublicationReasoningReview, ...]:
        directory = self._directory(workflow_id)
        if not directory.exists():
            return ()
        values = []
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            raise WorkflowPublicationReviewRepositoryError(
                f"Publication-review repository is unreadable: {error}"
            ) from error
        for path in entries:
            if (not path.is_file() or path.suffix != ".json"
                    or not _REVIEW_ID.fullmatch(path.stem)):
                raise WorkflowPublicationReviewRepositoryError(
                    "Publication-review repository contains an invalid entry."
                )
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise WorkflowPublicationReviewRepositoryError(
                    f"Publication-review record is unreadable: {error}"
                ) from error
            review = WorkflowPublicationReasoningReview.from_dict(value)
            if review.workflow_id != workflow_id:
                raise WorkflowPublicationReviewRepositoryError(
                    "Publication-review workflow identity is inconsistent."
                )
            values.append(review)
        return tuple(values)

    def _directory(self, workflow_id: str) -> Path:
        value = str(workflow_id or "").strip()
        if not _WORKFLOW_ID.fullmatch(value):
            raise WorkflowPublicationReviewRepositoryError("Workflow identity is invalid.")
        return self.root / value
