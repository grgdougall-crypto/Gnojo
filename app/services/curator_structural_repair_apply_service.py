from __future__ import annotations

import json
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
)
from app.repositories.structural_repair_approval_repository import (
    StructuralRepairApprovalRepository,
    StructuralRepairApprovalRepositoryError,
)
from app.services.curator_evidence_specification_catalog import (
    PRODUCTION_EVIDENCE_SPECIFICATIONS,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_contracts import StructuralRepairPlan
from app.services.curator_structural_repair_governance import (
    STAGE3_SCHEMA_VERSION,
    StructuralRepairApplicationRecord,
    StructuralRepairFingerprint,
)
from app.services.curator_structural_repair_preview_service import (
    CuratorStructuralRepairPreviewService,
)
from app.services.curator_task_service import CuratorTaskService
from app.services.workflow_draft_persistence import (
    WorkflowDraftPersistence,
    WorkflowDraftPersistenceError,
)
from app.services.workflow_validation_service import WorkflowValidationService
from curator.workflow_reasoning import WorkflowReasoningAuditor


class StructuralRepairApplyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CuratorStructuralRepairApplyService:
    """Supervised application of one stored approval to one editable draft."""

    ACTIONABLE = frozenset({"open", "in_progress", "deferred"})

    def __init__(self, repository_root: Path, *,
                 task_loader: Callable[[str], dict[str, Any]] | None = None,
                 specification_catalog=PRODUCTION_EVIDENCE_SPECIFICATIONS,
                 validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 lock_timeout: float = 2.0):
        self.root = Path(repository_root).resolve()
        self.approvals = StructuralRepairApprovalRepository(self.root / "curation_memory")
        self.applications = StructuralRepairApplicationRepository(self.root / "curation_memory")
        self.persistence = WorkflowDraftPersistence(self.root / "app" / "workflow_drafts")
        self.task_loader = task_loader or (lambda task_id: CuratorTaskService(self.root).get(task_id))
        self.catalog = specification_catalog
        self.validator = validator or self._validate_candidate
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock_timeout = lock_timeout
        self.preview_service = CuratorStructuralRepairPreviewService()

    def apply(self, approval_id: str, *, reviewer_identity: str,
              fix_session_id: str) -> dict[str, Any]:
        try:
            stored = self.approvals.get(approval_id)
        except StructuralRepairApprovalRepositoryError as error:
            category = "approval_missing" if "not found" in str(error).lower() else "approval_invalid"
            raise StructuralRepairApplyError(category, str(error)) from error
        approval = stored["approval"]
        prior = self.applications.get(approval.application_id)
        if prior and prior[-1].outcome == "applied":
            return self._already_applied(approval, prior[-1])
        if stored["state"] != "approved":
            raise StructuralRepairApplyError("approval_invalid", "Approval is no longer applicable.")
        if self.now() >= datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00")):
            self.approvals.transition(approval_id, "expired", "approval_expired")
            raise StructuralRepairApplyError("approval_expired", "Approval has expired.")
        if (approval.reviewer_identity != str(reviewer_identity or "").strip()
                or approval.fix_session_id != str(fix_session_id or "").strip()):
            raise StructuralRepairApplyError("approval_invalid", "Reviewer or session binding does not match.")
        if approval.workflow_lifecycle != "draft" or approval.workflow_path != (
                f"app/workflow_drafts/{approval.workflow_filename}"):
            raise StructuralRepairApplyError("approval_invalid", "Approval target is not an editable draft.")

        task = self._task(approval)
        specification = self.catalog.lookup(approval.specification_id.replace(
            "-windows-v" + str(approval.specification_version), ""), approval.specification_version)
        if specification is None:
            # Catalog lookup is evidence-key based; bind by immutable identity as a second safe path.
            specification = next((item for item in self.catalog.all()
                                  if item.specification_id == approval.specification_id
                                  and item.version == approval.specification_version), None)
        if specification is None or StructuralRepairFingerprint.contract(specification) != approval.specification_digest:
            self.approvals.transition(approval_id, "invalidated", "specification_changed")
            raise StructuralRepairApplyError("preview_unknown", "Approved evidence specification changed.")

        try:
            with self.persistence.locked(approval.workflow_filename,
                                         timeout=self.lock_timeout) as draft:
                before = draft.read()
                if before.raw_sha256 != approval.workflow_raw_sha256_before:
                    self.approvals.transition(approval_id, "invalidated", "stale_workflow")
                    raise StructuralRepairApplyError("stale_workflow", "Editable workflow changed after approval.")
                if (before.semantic_sha256 != approval.workflow_semantic_sha256_before
                        or before.workflow.get("workflow_id") != approval.workflow_id):
                    self.approvals.transition(approval_id, "invalidated", "stale_workflow")
                    raise StructuralRepairApplyError("stale_workflow", "Editable workflow identity changed.")
                task = self._task(approval)
                regenerated = CuratorRepairAdapterRegistry(
                    evidence_specs=self.catalog.all()
                ).preview(task, before.workflow)
                try:
                    self._match_approval(approval, stored["preview"], regenerated)
                except StructuralRepairApplyError:
                    self.approvals.transition(approval_id, "invalidated", "preview_changed")
                    raise
                plan = StructuralRepairPlan.from_dict(regenerated["plan"])
                candidate = self.preview_service.simulate(before.workflow, regenerated)
                self._assert_exact_graph(before.workflow, candidate, regenerated, plan)
                validation = self.validator(candidate, task)
                if not validation.get("passed"):
                    self._append(approval, regenerated, validation, outcome="failed",
                                 failure="validation_failed_prewrite")
                    self.approvals.transition(approval_id, "invalidated", "validation_failed_prewrite")
                    raise StructuralRepairApplyError("validation_failed_prewrite",
                                                     "Candidate workflow failed validation.")
                candidate_bytes = (json.dumps(candidate, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
                expected_raw = StructuralRepairFingerprint.raw_workflow(candidate_bytes)
                expected_semantic = StructuralRepairFingerprint.semantic_workflow(candidate)
                pending = self._append(approval, regenerated, validation, outcome="pending",
                                       expected_raw=expected_raw, expected_semantic=expected_semantic)
                try:
                    replacement = draft.replace(approval.workflow_raw_sha256_before, candidate_bytes)
                except WorkflowDraftPersistenceError as error:
                    category = "stale_workflow" if error.code == "stale_workflow" else "persistence_failed"
                    self._append_revision(pending, outcome="failed", failure=category,
                                          reason=str(error), validation=validation)
                    if category == "stale_workflow":
                        self.approvals.transition(approval_id, "invalidated", category)
                    raise StructuralRepairApplyError(category, str(error)) from error
                try:
                    persisted_validation = self.validator(replacement.after.workflow, task)
                    self._assert_exact_graph(before.workflow, replacement.after.workflow,
                                             regenerated, plan)
                    if (replacement.after.raw_sha256 != expected_raw
                            or replacement.after.semantic_sha256 != expected_semantic
                            or not persisted_validation.get("passed")):
                        raise StructuralRepairApplyError(
                            "validation_failed_postwrite", "Persisted workflow failed verification."
                        )
                except Exception as error:
                    return self._rollback(draft, before, replacement.after.raw_sha256,
                                          approval, pending, validation, error)
                final = self._append_revision(
                    pending, outcome="applied", failure="", reason="",
                    validation={"prewrite": validation, "postwrite": persisted_validation},
                    applied=True,
                )
                self.approvals.transition(approval_id, "consumed", "applied")
                return {"status": "applied", "application": final.to_dict(),
                        "workflow": replacement.after.workflow}
        except WorkflowDraftPersistenceError as error:
            if error.code == "lock_unavailable":
                raise StructuralRepairApplyError("lock_unavailable", str(error)) from error
            raise StructuralRepairApplyError("persistence_failed", str(error)) from error

    def classify_pending(self, application_id: str) -> str:
        history = self.applications.get(application_id)
        if not history or history[-1].outcome != "pending":
            return "not_pending"
        record = history[-1]
        with self.persistence.locked(Path(record.workflow_path).name,
                                     timeout=self.lock_timeout) as draft:
            current = draft.read().raw_sha256
        if current == record.workflow_raw_sha256_before:
            return "before_state"
        if current == record.expected_workflow_raw_sha256_after:
            return "expected_after_state"
        return "unexpected_state"

    def _task(self, approval):
        try:
            task = self.task_loader(approval.task_id)
        except Exception as error:
            raise StructuralRepairApplyError("approval_invalid", "Originating task is unavailable.") from error
        if task.get("status", "open") not in self.ACTIONABLE:
            raise StructuralRepairApplyError("approval_invalid", "Originating task is no longer actionable.")
        if task.get("finding_id", "") != approval.finding_id:
            raise StructuralRepairApplyError("approval_invalid", "Originating finding identity changed.")
        return task

    @staticmethod
    def _match_approval(approval, stored, regenerated):
        checks = (
            (StructuralRepairFingerprint.contract(stored), approval.preview_digest),
            (StructuralRepairFingerprint.contract(regenerated), approval.preview_digest),
            (StructuralRepairFingerprint.contract(regenerated.get("plan")), approval.plan_digest),
            (StructuralRepairFingerprint.contract(regenerated.get("specification")), approval.specification_digest),
        )
        if any(actual != expected for actual, expected in checks):
            raise StructuralRepairApplyError("preview_unknown", "Approved structural preview no longer matches.")

    @staticmethod
    def _assert_exact_graph(before, candidate, preview, plan):
        inserted = {item["node_id"] for item in preview["proposed"]["inserted_nodes"]}
        if set(candidate["nodes"]) != set(before["nodes"]) | inserted:
            raise StructuralRepairApplyError("plan_invalid", "Candidate node set exceeds the approved plan.")
        for node_id in set(before["nodes"]) - {edge.source for edge in plan.changed_existing_edges}:
            if candidate["nodes"][node_id] != before["nodes"][node_id]:
                raise StructuralRepairApplyError("plan_invalid", "An unaffected node changed.")

    @staticmethod
    def _validate_candidate(workflow, task):
        validation = WorkflowValidationService().validate(workflow)
        quality = validation.get("quality", {})
        reasoning = WorkflowReasoningAuditor().analyze(workflow)
        defect_present = any(item.rule == "CUR-WR-TERMINAL-EVIDENCE"
                             and item.node_id == task.get("structured_evidence", {}).get("terminal")
                             for item in reasoning)
        passed = bool(validation.get("is_valid")) and not validation.get("unreachable_nodes") \
            and quality.get("overall_status") != "ERROR" and not defect_present
        return {"passed": passed, "schema": validation, "quality": quality,
                "reasoning_finding_absent": not defect_present}

    def _append(self, approval, preview, validation, *, outcome, failure="",
                expected_raw="", expected_semantic=""):
        plan = StructuralRepairPlan.from_dict(preview["plan"])
        now = self.now().isoformat()
        return StructuralRepairApplicationRecord.from_dict(self.applications.append({
            "schema_version": STAGE3_SCHEMA_VERSION,
            "application_id": approval.application_id, "approval_id": approval.approval_id,
            "event_id": "SRE-" + secrets.token_hex(8).upper(), "revision": 1,
            "previous_event_digest": "", "task_id": approval.task_id,
            "finding_id": approval.finding_id, "fix_session_id": approval.fix_session_id,
            "reviewer_identity": approval.reviewer_identity,
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": approval.workflow_id, "workflow_path": approval.workflow_path,
            "workflow_raw_sha256_before": approval.workflow_raw_sha256_before,
            "workflow_semantic_sha256_before": approval.workflow_semantic_sha256_before,
            "expected_workflow_raw_sha256_after": expected_raw,
            "expected_workflow_semantic_sha256_after": expected_semantic,
            "preview_digest": approval.preview_digest, "plan_digest": approval.plan_digest,
            "adapter_id": approval.adapter_id, "specification_id": approval.specification_id,
            "specification_version": approval.specification_version,
            "specification_digest": approval.specification_digest,
            "proposed_node_ids": [
                plan.probe.evidence_node.node_id, plan.probe.result_node.node_id,
                *(item.node_id for item in plan.proposed_outcome_nodes),
            ],
            "changed_edges": [edge.__dict__ for edge in plan.changed_existing_edges],
            "new_edges": [edge.__dict__ for edge in plan.new_edges],
            "created_at": now, "applied_at": "", "finalized_at": now if outcome == "failed" else "",
            "validation_summaries": validation, "outcome": outcome,
            "failure_category": failure, "failure_reason": "",
            "rollback_status": "not_required", "rollback_raw_sha256": "",
            "rollback_semantic_sha256": "",
        }))

    def _append_revision(self, previous, *, outcome, failure, reason, validation,
                         applied=False, rollback_status="not_required", rollback=None):
        data = previous.to_dict()
        data.update({
            "event_id": "SRE-" + secrets.token_hex(8).upper(),
            "revision": previous.revision + 1, "previous_event_digest": previous.event_digest,
            "outcome": outcome, "failure_category": failure, "failure_reason": str(reason)[:1000],
            "validation_summaries": validation, "applied_at": self.now().isoformat() if applied else "",
            "finalized_at": self.now().isoformat(), "rollback_status": rollback_status,
            "rollback_raw_sha256": rollback.raw_sha256 if rollback else "",
            "rollback_semantic_sha256": rollback.semantic_sha256 if rollback else "",
        })
        return StructuralRepairApplicationRecord.from_dict(self.applications.append(data))

    def _rollback(self, draft, before, applied_raw, approval, pending, validation, error):
        try:
            restored = draft.restore(applied_raw, before.content).after
            final = self._append_revision(
                pending, outcome="rolled_back", failure="rollback_succeeded", reason=str(error),
                validation=validation, rollback_status="succeeded", rollback=restored,
            )
            self.approvals.transition(approval.approval_id, "invalidated", "rollback_succeeded")
            return {"status": "rollback_succeeded", "application": final.to_dict()}
        except Exception as rollback_error:
            final = self._append_revision(
                pending, outcome="failed", failure="rollback_failed", reason=str(rollback_error),
                validation=validation, rollback_status="failed",
            )
            self.approvals.transition(approval.approval_id, "invalidated", "rollback_failed")
            raise StructuralRepairApplyError("rollback_failed", str(rollback_error)) from error

    def _already_applied(self, approval, record):
        with self.persistence.locked(approval.workflow_filename, timeout=self.lock_timeout) as draft:
            current = draft.read()
        if current.raw_sha256 != record.expected_workflow_raw_sha256_after:
            raise StructuralRepairApplyError("approval_invalid", "Applied workflow no longer matches recorded state.")
        return {"status": "already_applied", "application": record.to_dict(),
                "workflow": deepcopy(current.workflow)}
