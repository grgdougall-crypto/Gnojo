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
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
    StructuralRepairRecoveryRepositoryError,
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

    ACTIONABLE = frozenset({"open", "in_progress"})

    def __init__(self, repository_root: Path):
        self.root = Path(repository_root).resolve()
        self.approvals = StructuralRepairApprovalRepository(self.root / "curation_memory")
        self.applications = StructuralRepairApplicationRepository(self.root / "curation_memory")
        self.recoveries = StructuralRepairRecoveryRepository(self.root / "curation_memory")
        self.persistence = WorkflowDraftPersistence(self.root / "app" / "workflow_drafts")
        self.task_loader = lambda task_id: CuratorTaskService(self.root).get(task_id)
        self.catalog = PRODUCTION_EVIDENCE_SPECIFICATIONS
        self.validator = self._validate_candidate
        self.now = lambda: datetime.now(timezone.utc)
        self.lock_timeout = 2.0
        self.preview_service = CuratorStructuralRepairPreviewService()

    @classmethod
    def _for_test(cls, repository_root: Path, *,
                  task_loader: Callable[[str], dict[str, Any]],
                  specification_catalog=PRODUCTION_EVIDENCE_SPECIFICATIONS,
                  validator: Callable[..., dict[str, Any]] | None = None,
                  now: Callable[[], datetime] | None = None, lock_timeout: float = 2.0):
        """Explicit test-only construction; production construction is code-owned."""
        value = cls.__new__(cls)
        value.root = Path(repository_root).resolve()
        value.approvals = StructuralRepairApprovalRepository(value.root / "curation_memory")
        value.applications = StructuralRepairApplicationRepository(value.root / "curation_memory")
        value.recoveries = StructuralRepairRecoveryRepository(value.root / "curation_memory")
        value.persistence = WorkflowDraftPersistence(value.root / "app" / "workflow_drafts")
        value.task_loader = task_loader
        value.catalog = specification_catalog
        value.validator = validator or value._validate_candidate
        value.now = now or (lambda: datetime.now(timezone.utc))
        value.lock_timeout = lock_timeout
        value.preview_service = CuratorStructuralRepairPreviewService()
        return value

    def apply(self, approval_id: str, *, reviewer_identity: str,
              fix_session_id: str) -> dict[str, Any]:
        try:
            stored = self.approvals.get(approval_id)
        except StructuralRepairApprovalRepositoryError as error:
            category = "approval_missing" if "not found" in str(error).lower() else "approval_invalid"
            raise StructuralRepairApplyError(category, str(error)) from error
        approval = stored["approval"]
        if (approval.reviewer_identity != str(reviewer_identity or "").strip()
                or approval.fix_session_id != str(fix_session_id or "").strip()):
            raise StructuralRepairApplyError("approval_invalid", "Reviewer or session binding does not match.")
        if approval.workflow_lifecycle != "draft" or approval.workflow_path != (
                f"app/workflow_drafts/{approval.workflow_filename}"):
            raise StructuralRepairApplyError("approval_invalid", "Approval target is not an editable draft.")

        task = self._task(approval)
        registration = CuratorRepairAdapterRegistry().lookup(
            task.get("curator_rule", ""), task.get("finding_type", "")
        )
        if (not registration or not registration.structural
                or registration.adapter_id != approval.adapter_id):
            self._invalidate_approval(approval_id, "adapter_changed")
            raise StructuralRepairApplyError("approval_invalid", "Approved adapter identity no longer matches.")
        prior = self.applications.get(approval.application_id)
        if prior and prior[-1].outcome == "applied":
            return self._already_applied(approval, prior[-1])
        if stored["state"] != "approved":
            raise StructuralRepairApplyError("approval_invalid", "Approval is no longer applicable.")
        if self.now() >= datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00")):
            self.approvals.transition(approval_id, "expired", "approval_expired")
            raise StructuralRepairApplyError("approval_expired", "Approval has expired.")
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
                current_history = self.applications.get(approval.application_id)
                if current_history and current_history[-1].outcome == "applied":
                    return self._already_applied_snapshot(approval, current_history[-1], before)
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
                reasoning_baseline = self._reasoning_findings(before.workflow)
                candidate = self.preview_service.simulate(before.workflow, regenerated)
                self._assert_exact_graph(before.workflow, candidate, regenerated, plan)
                validation = self.validator(candidate, task, reasoning_baseline)
                if not validation.get("passed"):
                    self._append(approval, regenerated, validation, outcome="failed",
                                 failure="validation_failed_prewrite")
                    self.approvals.transition(approval_id, "invalidated", "validation_failed_prewrite")
                    raise StructuralRepairApplyError("validation_failed_prewrite",
                                                     "Candidate workflow failed validation.")
                candidate_bytes = (json.dumps(candidate, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
                expected_raw = StructuralRepairFingerprint.raw_workflow(candidate_bytes)
                expected_semantic = StructuralRepairFingerprint.semantic_workflow(candidate)
                try:
                    self.recoveries.capture(
                        application_id=approval.application_id,
                        approval_id=approval.approval_id, task_id=approval.task_id,
                        finding_id=approval.finding_id,
                        fix_session_id=approval.fix_session_id,
                        reviewer_identity=approval.reviewer_identity,
                        workflow_id=approval.workflow_id,
                        workflow_path=approval.workflow_path,
                        original_bytes=before.content,
                        raw_before=before.raw_sha256,
                        semantic_before=before.semantic_sha256,
                        expected_raw_after=expected_raw,
                        expected_semantic_after=expected_semantic,
                        captured_at=self.now().isoformat(),
                    )
                except StructuralRepairRecoveryRepositoryError as error:
                    self.approvals.transition(
                        approval_id, "invalidated", "recovery_capture_failed"
                    )
                    raise StructuralRepairApplyError(
                        "persistence_failed",
                        "Exact-byte recovery material could not be retained; no draft write occurred.",
                    ) from error
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
                    persisted_validation = self.validator(
                        replacement.after.workflow, task, reasoning_baseline
                    )
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
                try:
                    final = self._append_revision(
                        pending, outcome="applied", failure="", reason="",
                        validation={"prewrite": validation, "postwrite": persisted_validation},
                        applied=True,
                    )
                except Exception as error:
                    return self._rollback(
                        draft, before, replacement.after.raw_sha256, approval, pending,
                        {"prewrite": validation, "postwrite": persisted_validation}, error,
                        provenance_impaired=True,
                    )
                approval_finalization = self._consume_approval(approval_id)
                return {"status": "applied", "application": final.to_dict(),
                        "workflow": replacement.after.workflow,
                        "approval_finalization": approval_finalization}
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

    def _assert_exact_graph(self, before, candidate, preview, plan):
        inserted = {item["node_id"] for item in preview["proposed"]["inserted_nodes"]}
        if set(candidate["nodes"]) != set(before["nodes"]) | inserted:
            raise StructuralRepairApplyError("plan_invalid", "Candidate node set exceeds the approved plan.")
        inserted_by_id = {item["node_id"]: item["content"]
                          for item in preview["proposed"]["inserted_nodes"]}
        for node_id, content in inserted_by_id.items():
            if candidate["nodes"].get(node_id) != content:
                raise StructuralRepairApplyError("plan_invalid", "A proposed node differs from approval.")
        changed_sources = {edge.source for edge in plan.changed_existing_edges}
        for node_id in set(before["nodes"]) - changed_sources:
            if candidate["nodes"][node_id] != before["nodes"][node_id]:
                raise StructuralRepairApplyError("plan_invalid", "An unaffected node changed.")
        for source in changed_sources:
            expected_node = deepcopy(before["nodes"][source])
            expected_workflow = {"nodes": {source: expected_node}}
            for change in preview["proposed"]["changed_predecessor_edges"]:
                if change["before"]["source"] == source:
                    self.preview_service._replace_edge(
                        expected_workflow, change["before"], change["after"]
                    )
            if candidate["nodes"].get(source) != expected_node:
                raise StructuralRepairApplyError(
                    "plan_invalid", "A changed source contains an unapproved modification."
                )
        before_edges = set(self._workflow_edges(before))
        candidate_edges = set(self._workflow_edges(candidate))
        removed = {(edge.source, edge.route, edge.destination)
                   for edge in plan.changed_existing_edges}
        redirected = {
            (item["after"]["source"], item["after"]["route"], item["after"]["destination"])
            for item in preview["proposed"]["changed_predecessor_edges"]
        }
        new_edges = {
            (edge.source, self._runtime_route(candidate, edge.source, edge.route), edge.destination)
            for edge in plan.new_edges
        }
        if candidate_edges != (before_edges - removed) | redirected | new_edges:
            raise StructuralRepairApplyError("plan_invalid", "Candidate edge delta differs from approval.")
        approved_unaffected = {(edge.source, edge.route, edge.destination)
                               for edge in plan.unaffected_routes}
        if approved_unaffected != before_edges - removed:
            raise StructuralRepairApplyError("plan_invalid", "Unaffected routes differ from approval.")

    @classmethod
    def _reasoning_findings(cls, workflow):
        return frozenset(cls._reasoning_identity(item)
                         for item in WorkflowReasoningAuditor().analyze(workflow))

    @staticmethod
    def _reasoning_identity(item):
        return (item.rule, item.finding_type, item.node_id, item.title)

    @staticmethod
    def _workflow_edges(workflow):
        from curator.workflow_reasoning import WorkflowGraph
        graph = WorkflowGraph(workflow)
        return [(source, route, destination) for source in workflow.get("nodes", {})
                for route, destination in graph.transitions(source)]

    @staticmethod
    def _runtime_route(workflow, source, route):
        node = workflow.get("nodes", {}).get(source, {})
        answer = (node.get("answers") or {}).get(route) if isinstance(node, dict) else None
        return str(answer.get("label") or route) if isinstance(answer, dict) else route

    @classmethod
    def _validate_candidate(cls, workflow, task, reasoning_baseline):
        validation = WorkflowValidationService().validate(workflow)
        quality = validation.get("quality", {})
        reasoning = cls._reasoning_findings(workflow)
        terminal = task.get("structured_evidence", {}).get("terminal")
        defect_present = any(item[0] == "CUR-WR-TERMINAL-EVIDENCE" and item[2] == terminal
                             for item in reasoning)
        new_reasoning = reasoning - frozenset(reasoning_baseline)
        passed = bool(validation.get("is_valid")) and not validation.get("unreachable_nodes") \
            and quality.get("overall_status") != "ERROR" and not defect_present and not new_reasoning
        return {"passed": passed, "schema": validation, "quality": quality,
                "reasoning_finding_absent": not defect_present,
                "new_reasoning_findings": [list(item) for item in sorted(new_reasoning)]}

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

    def _rollback(self, draft, before, applied_raw, approval, pending, validation, error,
                  provenance_impaired=False):
        try:
            restored = draft.restore(applied_raw, before.content).after
            if (restored.raw_sha256 != before.raw_sha256
                    or restored.semantic_sha256 != before.semantic_sha256):
                raise StructuralRepairApplyError("rollback_failed", "Restored draft fingerprint differs.")
        except Exception as rollback_error:
            try:
                self._append_revision(
                    pending, outcome="failed", failure="rollback_failed", reason=str(rollback_error),
                    validation=validation, rollback_status="failed",
                )
            except Exception:
                pass
            self._invalidate_approval(approval.approval_id, "rollback_failed")
            raise StructuralRepairApplyError("rollback_failed", str(rollback_error)) from error

        final = None
        provenance_error = None
        try:
            final = self._append_revision(
                pending, outcome="rolled_back", failure="rollback_succeeded", reason=str(error),
                validation=validation, rollback_status="succeeded", rollback=restored,
            )
        except Exception as journal_error:
            provenance_error = journal_error
        self._invalidate_approval(approval.approval_id, "rollback_succeeded")
        if provenance_error or provenance_impaired:
            raise StructuralRepairApplyError(
                "rollback_succeeded",
                "Workflow was restored exactly, but application provenance could not be finalized."
            ) from error
        return {"status": "rollback_succeeded", "application": final.to_dict()}

    def _consume_approval(self, approval_id):
        try:
            self.approvals.transition(approval_id, "consumed", "applied")
            return "consumed"
        except Exception:
            return "pending_recovery"

    def _invalidate_approval(self, approval_id, reason):
        try:
            self.approvals.transition(approval_id, "invalidated", reason)
        except Exception:
            pass

    def _already_applied(self, approval, record):
        with self.persistence.locked(approval.workflow_filename, timeout=self.lock_timeout) as draft:
            current = draft.read()
        return self._already_applied_snapshot(approval, record, current)

    def _already_applied_snapshot(self, approval, record, current):
        if current.raw_sha256 != record.expected_workflow_raw_sha256_after:
            raise StructuralRepairApplyError("approval_invalid", "Applied workflow no longer matches recorded state.")
        approval_finalization = self._consume_approval(approval.approval_id)
        return {"status": "already_applied", "application": record.to_dict(),
                "workflow": deepcopy(current.workflow),
                "approval_finalization": approval_finalization}
