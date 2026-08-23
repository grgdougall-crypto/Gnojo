from __future__ import annotations

import hashlib
import hmac
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.services.curator_relationship_repair_proposal_service import (
    CuratorRelationshipRepairProposalService,
)
from app.services.curator_targeted_verification_service import (
    CuratorTargetedVerificationService,
)
from curator.checks import CuratorChecks
from curator.inventory import CuratorInventory
from curator.memory import CuratorMemoryStore


class CuratorRelationshipRepairApplicationError(RuntimeError):
    """One reviewed relationship proposal could not be applied safely."""


class CuratorRelationshipRepairApplicationService:
    """Apply one freshly revalidated deterministic relationship proposal."""

    ACTIONABLE_STATUSES = {"open", "in_progress", "deferred"}
    ELIGIBLE_OUTCOMES = {"add_reciprocal", "remove_unsupported"}

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.verifier = CuratorTargetedVerificationService(self.root)

    def approval_token(self, task: dict[str, Any], proposal: dict[str, Any]) -> str:
        path = self._proposal_path(proposal)
        try:
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            content_hash = "unavailable"
        value = {
            "task": {key: task.get(key) for key in (
                "task_id", "status", "finding_type", "content_type", "content_identifier"
            )},
            "proposal": {key: proposal.get(key) for key in (
                "outcome", "command_id", "article_id", "command_declares_article",
                "article_declares_command", "metadata_change", "affected_record", "affected_field",
            )},
            "record_sha256": content_hash,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def apply(self, task_id: str, *, approval_token: str, approved: bool) -> dict[str, Any]:
        if not approved:
            raise CuratorRelationshipRepairApplicationError("Explicit reviewer approval is required.")
        task, proposal = self._current_proposal(task_id)
        if task.get("status") not in self.ACTIONABLE_STATUSES:
            raise CuratorRelationshipRepairApplicationError("Only an actionable relationship task can be applied.")
        if proposal.get("outcome") not in self.ELIGIBLE_OUTCOMES:
            raise CuratorRelationshipRepairApplicationError(
                "This proposal requires human analysis and has no deterministic mutation."
            )
        expected_token = self.approval_token(task, proposal)
        if not approval_token or not hmac.compare_digest(approval_token, expected_token):
            raise CuratorRelationshipRepairApplicationError(
                "The proposal or authoritative record changed. Refresh and review it again."
            )

        path = self._proposal_path(proposal)
        memory_path = self.store.state_path
        snapshots = {
            path: path.read_bytes(),
            memory_path: memory_path.read_bytes() if memory_path.exists() else None,
        }
        try:
            record = json.loads(snapshots[path].decode("utf-8"))
            before, after = self._mutated_declarations(record, proposal)
            self._write_json(path, record)
            self._validate_persisted(task, proposal, path, after)
            verification = self.verifier.verify(task_id)
            if verification.get("status") != "appears_corrected":
                raise CuratorRelationshipRepairApplicationError(
                    "Targeted verification did not confirm that the original finding was corrected."
                )
            self.store.update_task(
                task_id,
                actor="Human relationship reviewer",
                event_name="relationship_repair_proposal_applied",
                note=proposal["metadata_change"],
                metadata={
                    "proposal_outcome": proposal["outcome"],
                    "reviewer_approved": True,
                    "affected_record": proposal["affected_record"],
                    "affected_field": proposal["affected_field"],
                    "before_declarations": before,
                    "after_declarations": after,
                    "verification_result": verification.get("status"),
                    "task_status_preserved": task.get("status"),
                },
            )
        except Exception:
            self._restore(snapshots)
            raise
        return {
            "applied": True,
            "task_id": task_id,
            "proposal": deepcopy(proposal),
            "before_declarations": before,
            "after_declarations": after,
            "verification": verification,
            "task_status": task.get("status"),
        }

    def _current_proposal(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        task = self.store.load().get("tasks", {}).get(task_id)
        if not task:
            raise CuratorRelationshipRepairApplicationError("Knowledge Task was not found.")
        relationship = self.verifier.relationship_evidence(task)
        proposal = CuratorRelationshipRepairProposalService().build(task, relationship)
        if not proposal:
            raise CuratorRelationshipRepairApplicationError(
                "This task does not have a supported relationship repair proposal."
            )
        return task, proposal

    def _proposal_path(self, proposal: dict[str, Any]) -> Path:
        relative = str(proposal.get("affected_record") or "").replace("\\", "/")
        if not relative or Path(relative).is_absolute():
            raise CuratorRelationshipRepairApplicationError("The proposal has no safe authoritative record path.")
        path = (self.root / relative).resolve()
        allowed = ((self.root / "knowledge_base" / "commands").resolve(),
                   (self.root / "knowledge_base" / "published").resolve())
        if not any(path.parent == directory for directory in allowed):
            raise CuratorRelationshipRepairApplicationError("The proposed record is outside the supported content stores.")
        if not path.is_file():
            raise CuratorRelationshipRepairApplicationError("The proposed authoritative record is unavailable.")
        return path

    @staticmethod
    def _mutated_declarations(record: dict[str, Any], proposal: dict[str, Any]) -> tuple[list[str], list[str]]:
        field = proposal.get("affected_field")
        if field not in {"related_articles", "related_commands"}:
            raise CuratorRelationshipRepairApplicationError("The proposal targets an unsupported metadata field.")
        expected_record_id = (proposal.get("command_id") if field == "related_articles"
                              else proposal.get("article_id"))
        record_id = record.get("canonical_id") or record.get("id")
        if not expected_record_id or record_id != expected_record_id:
            raise CuratorRelationshipRepairApplicationError(
                "The authoritative record identity does not match the approved proposal."
            )
        values = record.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise CuratorRelationshipRepairApplicationError("The relationship field is malformed.")
        identifier = (proposal.get("article_id") if field == "related_articles"
                      else proposal.get("command_id"))
        identifier = str(identifier or "")
        if not identifier:
            raise CuratorRelationshipRepairApplicationError("The proposal relationship identity is missing.")
        before = list(values)
        outcome = proposal.get("outcome")
        if outcome == "add_reciprocal":
            if identifier in values:
                raise CuratorRelationshipRepairApplicationError("The proposed relationship is already present.")
            values.append(identifier)
        elif outcome == "remove_unsupported":
            if values.count(identifier) != 1:
                raise CuratorRelationshipRepairApplicationError("The proposed relationship is absent or ambiguous.")
            values.remove(identifier)
        else:
            raise CuratorRelationshipRepairApplicationError("The proposal outcome cannot be applied.")
        return before, list(values)

    def _validate_persisted(self, task: dict[str, Any], proposal: dict[str, Any],
                            path: Path, expected: list[str]) -> None:
        persisted = json.loads(path.read_text(encoding="utf-8"))
        if persisted.get(proposal["affected_field"]) != expected:
            raise CuratorRelationshipRepairApplicationError("The persisted relationship did not match the approved proposal.")
        inventory = CuratorInventory(self.root).collect()
        exact = [finding for finding in CuratorChecks(self.root).relationship_findings(inventory)
                 if finding.rule == task.get("curator_rule")
                 and finding.content_identifier == task.get("content_identifier")
                 and finding.finding_type == task.get("finding_type")]
        if exact:
            raise CuratorRelationshipRepairApplicationError("The canonical relationship finding remains after validation.")

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _restore(snapshots: dict[Path, bytes | None]) -> None:
        for path, content in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
