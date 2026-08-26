from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
    StructuralRepairApplicationRepositoryError,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from app.services.curator_structural_repair_preview_service import (
    CuratorStructuralRepairPreviewService,
    StructuralRepairPreviewError,
)
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.workflow_validation_service import WorkflowValidationService
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.workflow_reasoning import WorkflowReasoningAuditor


@dataclass(frozen=True)
class AutoRepairPolicyGateResult:
    gate_id: str
    label: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class ProgressAutoRepairPolicyResult:
    eligible: bool
    policy_id: str
    policy_version: int
    task_id: str
    finding_id: str
    workflow_id: str
    curator_rule: str
    finding_type: str
    adapter_id: str
    specification_id: str
    gate_results: tuple[AutoRepairPolicyGateResult, ...]
    failed_gate_reasons: tuple[str, ...]
    proposed_mutation: dict[str, Any]
    evaluated_fingerprints: dict[str, str]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = "ELIGIBLE" if self.eligible else "INELIGIBLE"
        return value


class CuratorProgressAutoRepairPolicyService:
    """Read-only policy projection for one allowlisted progress metadata repair."""

    POLICY_ID = "cur-wr-progress-draft-auto-apply-eligibility"
    POLICY_VERSION = 1
    RULE = "CUR-WR-PROGRESS"
    FINDING_TYPE = "workflow_reasoning_progress_inconsistency"
    ADAPTER_ID = "branch_aware_progress_metadata"
    SPECIFICATION_ID = "branch-aware-progress-metadata-v1"
    ACTIONABLE = frozenset({"open", "in_progress"})
    EXPECTED_PRE_QUALITY = frozenset({
        "PREMATURE_STATIC_PROGRESS",
        "STATIC_PATH_LENGTH_CONFLICT",
    })

    def __init__(
        self,
        repository_root: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        application_repository: StructuralRepairApplicationRepository | None = None,
    ):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.curator_root = self.root / "curation_memory"
        self.store = CuratorMemoryStore(self.curator_root)
        self.lifecycle = CuratorWorkflowLifecycleService(self.root)
        self.registry = CuratorRepairAdapterRegistry()
        self.preview_service = CuratorStructuralRepairPreviewService()
        self.applications = application_repository or StructuralRepairApplicationRepository(
            self.curator_root
        )
        self.recoveries = StructuralRepairRecoveryRepository(self.curator_root)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def evaluate(self, task_id: str) -> ProgressAutoRepairPolicyResult:
        try:
            task = self.store.load().get("tasks", {}).get(str(task_id or ""))
        except CuratorMemoryError:
            task = None
        return self._evaluate_task(task if isinstance(task, dict) else {}, str(task_id or ""))

    def _evaluate_task(
        self, task: dict[str, Any], requested_task_id: str,
    ) -> ProgressAutoRepairPolicyResult:
        gates: list[AutoRepairPolicyGateResult] = []

        def add(gate_id: str, label: str, passed: bool, success: str, failure: str) -> None:
            gates.append(AutoRepairPolicyGateResult(
                gate_id, label, bool(passed), success if passed else failure,
            ))

        task_id = str(task.get("task_id") or requested_task_id)
        finding_id = str(task.get("finding_id") or "")
        workflow_id = str(task.get("content_identifier") or "")
        rule = str(task.get("curator_rule") or "")
        finding_type = str(task.get("finding_type") or "")
        registration = self.registry.lookup(rule, finding_type)
        specification = self.registry.progress_metadata_specification(rule, finding_type)
        exact_identity = bool(
            task
            and rule == self.RULE
            and finding_type == self.FINDING_TYPE
            and task.get("content_type") == "workflow"
            and registration
            and registration.adapter_id == self.ADAPTER_ID
            and not registration.executable
            and registration.structural
            and registration.supervised_apply_available
            and specification
            and getattr(specification, "specification_id", "") == self.SPECIFICATION_ID
            and getattr(specification, "version", 0) == 1
            and getattr(specification, "curator_rule", "") == self.RULE
            and getattr(specification, "finding_type", "") == self.FINDING_TYPE
            and getattr(specification, "metadata_path", "") == "/progress_mode"
            and tuple(getattr(specification, "allowed_before_states", ()))
            == ("absent", "static")
            and getattr(specification, "after_value", "") == "branch_aware"
            and getattr(specification, "approved", False) is True
        )

        publication_root = self.root / "app" / "workflow_publications"
        target = (
            self.lifecycle.resolve(workflow_id)
            if self._safe_id(workflow_id) and publication_root.is_dir()
            else None
        )
        draft_path = self.root / str(target.source_path) if target else None
        draft_raw = b""
        draft: dict[str, Any] | None = None
        if (target and target.lifecycle == "draft" and draft_path
                and draft_path.is_file() and self._within_drafts(draft_path)):
            try:
                draft_raw = draft_path.read_bytes()
                draft = target.workflow
            except OSError:
                draft = None
        draft_raw_sha = (
            StructuralRepairFingerprint.raw_workflow(draft_raw) if draft_raw else ""
        )
        draft_semantic_sha = (
            StructuralRepairFingerprint.semantic_workflow(draft) if draft else ""
        )

        publication, publication_version = self._active_publication(workflow_id)
        publication_semantic_sha = (
            StructuralRepairFingerprint.semantic_workflow(publication) if publication else ""
        )

        verification = task.get("current_verification")
        verification = verification if isinstance(verification, dict) else {}
        verification_fingerprint = str(verification.get("affected_fingerprint") or "")
        verification_current = bool(
            verification.get("status") == "still_detected"
            and verification.get("rule") == self.RULE
            and verification.get("workflow_id") == workflow_id
            and verification_fingerprint
            and verification_fingerprint == draft_semantic_sha
        )

        preview: dict[str, Any] = {}
        if exact_identity and draft:
            try:
                preview = self.registry.preview(
                    task,
                    draft,
                    workflow_raw_sha256=draft_raw_sha,
                    workflow_semantic_sha256=draft_semantic_sha,
                )
            except (KeyError, TypeError, ValueError, StructuralRepairPreviewError):
                preview = {}
        preview_available = bool(
            preview.get("available")
            and preview.get("read_only")
            and preview.get("adapter_id") == self.ADAPTER_ID
            and isinstance(preview.get("specification"), dict)
            and preview["specification"].get("specification_id")
            == self.SPECIFICATION_ID
        )
        candidate = None
        if preview_available and draft:
            try:
                candidate = self.preview_service.simulate(draft, preview)
            except (KeyError, TypeError, ValueError, StructuralRepairPreviewError):
                candidate = None

        mode_present = bool(draft is not None and "progress_mode" in draft)
        mode = draft.get("progress_mode") if draft else None
        allowed_mode = bool(draft is not None and (
            (not mode_present and mode is None) or (mode_present and mode == "static")
        ))
        proposed_exact = bool(
            specification
            and getattr(specification, "metadata_path", "") == "/progress_mode"
            and getattr(specification, "after_value", "") == "branch_aware"
        )

        expected_candidate = dict(draft) if draft else None
        if expected_candidate is not None:
            expected_candidate["progress_mode"] = "branch_aware"
        estimated_unchanged = bool(
            candidate is not None and draft is not None
            and candidate.get("estimated_steps") == draft.get("estimated_steps")
        )
        exact_delta = bool(
            candidate is not None and draft is not None
            and candidate == expected_candidate
            and candidate.get("nodes") == draft.get("nodes")
        )

        pre_validation = WorkflowValidationService().validate(draft) if draft else {}
        post_validation = WorkflowValidationService().validate(candidate) if candidate else {}
        pre_quality = pre_validation.get("quality") or {}
        post_quality = post_validation.get("quality") or {}
        pre_quality_rules = {
            str(item.get("rule") or "") for item in pre_quality.get("findings", [])
        }
        post_quality_rules = {
            str(item.get("rule") or "") for item in post_quality.get("findings", [])
        }
        pre_clean_except_target = bool(
            pre_validation.get("is_valid")
            and not pre_validation.get("unreachable_nodes")
            and pre_quality_rules
            and pre_quality_rules <= self.EXPECTED_PRE_QUALITY
        )
        post_clean = bool(
            post_validation.get("is_valid")
            and not post_validation.get("unreachable_nodes")
            and post_quality.get("overall_status") == "CLEAN"
            and not post_quality_rules
        )

        auditor = WorkflowReasoningAuditor()
        before_reasoning = auditor.analyze(draft) if draft else []
        after_reasoning = auditor.analyze(candidate) if candidate else []
        progress_absent = bool(
            candidate is not None
            and not any(item.rule == self.RULE for item in after_reasoning)
        )
        before_reasoning_signatures = {
            (item.rule, item.finding_type, item.node_id) for item in before_reasoning
        }
        after_reasoning_signatures = {
            (item.rule, item.finding_type, item.node_id) for item in after_reasoning
        }
        no_new_findings = bool(
            candidate is not None
            and not (post_quality_rules - pre_quality_rules)
            and not (after_reasoning_signatures - before_reasoning_signatures)
            and not (preview.get("validation", {}).get("new_reasoning_findings") or [])
        )

        recovery_available = bool(
            self.curator_root.is_dir()
            and os.access(self.curator_root, os.W_OK)
            and callable(getattr(self.recoveries, "capture", None))
        )
        journal_readable, ambiguous_application, application_reason = (
            self._application_state(task_id, finding_id, workflow_id)
        )
        journal_available = bool(
            journal_readable
            and self.curator_root.is_dir()
            and os.access(self.curator_root, os.W_OK)
            and callable(getattr(self.applications, "append", None))
        )

        add("01", "Exact policy identity", exact_identity,
            "Rule, finding, adapter, and approved specification match exactly.",
            "Rule, finding, adapter, or approved specification does not match the policy.")
        add("02", "Actionable task", task.get("status") in self.ACTIONABLE,
            "Task is actionable under existing Curator lifecycle rules.",
            "Task is missing or is not Open/In Progress.")
        add("03", "Current deterministic verification", verification_current,
            "Current targeted verification is still_detected and matches the draft fingerprint.",
            "A current still_detected verification with the exact draft fingerprint is unavailable.")
        add("04", "Canonical workflow resolution", bool(
            target and target.workflow_id == workflow_id
        ), "Canonical lifecycle resolution returned the exact workflow.",
            "The exact workflow could not be resolved canonically.")
        add("05", "Editable draft", bool(draft and target and target.lifecycle == "draft"),
            "The canonical workflow target is an editable draft.",
            "An authoritative editable draft is unavailable.")
        add("06", "No unrelated draft drift", bool(
            draft and publication and draft_semantic_sha == publication_semantic_sha
        ), f"Draft semantically matches active publication v{publication_version}.",
            "Draft does not exactly match an active publication or contains unrelated drift.")
        add("07", "Supported current progress mode", allowed_mode,
            "Current progress_mode is absent or explicitly static.",
            "Current progress_mode is missing ambiguously, already branch-aware, or unsupported.")
        add("08", "Exact proposed value", proposed_exact,
            "Approved specification proposes only branch_aware.",
            "The approved specification does not propose the exact allowlisted value.")
        add("09", "Estimated steps preserved", estimated_unchanged,
            "Simulation preserves estimated_steps exactly.",
            "Simulation is unavailable or changes estimated_steps.")
        add("10", "Zero graph/content/other metadata delta", exact_delta,
            "Simulation changes only /progress_mode and preserves the graph and all other content.",
            "Simulation is unavailable or contains a mutation outside /progress_mode.")
        add("11", "Trusted preview generation", preview_available,
            "The registered typed read-only preview succeeds.",
            "The trusted typed preview is unavailable or has mismatched identity.")
        add("12", "Clean pre/post validation", pre_clean_except_target and post_clean,
            "Pre-state contains only the expected progress defect and post-state validates cleanly.",
            "Pre-state has unrelated validation findings or post-state is not clean.")
        add("13", "Progress finding removed", progress_absent,
            "CUR-WR-PROGRESS is absent after simulation.",
            "CUR-WR-PROGRESS remains or simulation is unavailable.")
        add("14", "No new findings", no_new_findings,
            "Simulation introduces no new quality or reasoning findings.",
            "Simulation introduces or cannot exclude new quality/reasoning findings.")
        add("15", "Recovery capture capability", recovery_available,
            "Exact-byte recovery capture boundary is available.",
            "Exact-byte recovery capture capability is unavailable.")
        add("16", "Application journal capability", journal_available,
            "Append-only application journal boundary is available and readable.",
            "Application journal capability is unavailable or malformed.")
        add("17", "Unambiguous application state", not ambiguous_application,
            "No pending or applied transaction exists for this exact task/finding/workflow.",
            application_reason)
        add("18", "Generic execution disabled", bool(
            registration and not registration.executable
        ), "Generic executable authority remains disabled.",
            "Generic execution is enabled or adapter authority is unavailable.")

        failed = tuple(f"{item.gate_id}: {item.reason}" for item in gates if not item.passed)
        return ProgressAutoRepairPolicyResult(
            eligible=not failed,
            policy_id=self.POLICY_ID,
            policy_version=self.POLICY_VERSION,
            task_id=task_id,
            finding_id=finding_id,
            workflow_id=workflow_id,
            curator_rule=rule,
            finding_type=finding_type,
            adapter_id=registration.adapter_id if registration else "",
            specification_id=specification.specification_id if specification else "",
            gate_results=tuple(gates),
            failed_gate_reasons=failed,
            proposed_mutation={
                "path": "/progress_mode",
                "before_present": mode_present if draft is not None else None,
                "before_value": mode if draft is not None else None,
                "after_value": "branch_aware",
                "estimated_steps_unchanged": estimated_unchanged,
                "workflow_graph_unchanged": exact_delta,
                "publication_unchanged": True,
                "task_lifecycle_unchanged": True,
                "preview_confirmed": preview_available,
            },
            evaluated_fingerprints={
                "draft_raw_sha256": draft_raw_sha,
                "draft_semantic_sha256": draft_semantic_sha,
                "publication_semantic_sha256": publication_semantic_sha,
                "verification_affected_fingerprint": verification_fingerprint,
                "simulated_semantic_sha256": (
                    StructuralRepairFingerprint.semantic_workflow(candidate)
                    if candidate else ""
                ),
                "graph_sha256": (
                    StructuralRepairFingerprint.contract(draft.get("nodes"))
                    if draft else ""
                ),
            },
            timestamp=self.now().isoformat(),
        )

    def _active_publication(self, workflow_id: str) -> tuple[dict[str, Any] | None, int | None]:
        if not self._safe_id(workflow_id):
            return None, None
        directory = self.root / "app" / "workflow_publications" / workflow_id
        try:
            manifest = self._read_json(directory / "current.json")
            version = manifest.get("current_version")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                return None, None
            snapshot = self._read_json(directory / f"v{version:04d}.json")
            workflow = snapshot.get("workflow")
            if (not isinstance(workflow, dict)
                    or workflow.get("workflow_id") != workflow_id
                    or snapshot.get("publication", {}).get("version") != version):
                return None, None
            return workflow, version
        except (OSError, ValueError):
            return None, None

    def _application_state(
        self, task_id: str, finding_id: str, workflow_id: str,
    ) -> tuple[bool, bool, str]:
        try:
            application_ids = self.applications.list_application_ids()
            for application_id in application_ids:
                history = self.applications.get(application_id)
                if not history:
                    continue
                latest = history[-1]
                if (latest.task_id == task_id and latest.finding_id == finding_id
                        and latest.workflow_id == workflow_id
                        and latest.outcome in {"pending", "applied"}):
                    return True, True, (
                        "An existing pending or applied transaction makes execution ambiguous."
                    )
            return True, False, ""
        except (OSError, ValueError, StructuralRepairApplicationRepositoryError):
            return False, True, "Application history is unavailable or malformed."

    def _within_drafts(self, path: Path) -> bool:
        try:
            path.resolve().relative_to((self.root / "app" / "workflow_drafts").resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _safe_id(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]+", str(value or "")))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON record must be an object.")
        return value
