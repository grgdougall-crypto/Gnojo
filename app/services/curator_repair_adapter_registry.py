from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.services.curator_structural_repair_contracts import (
    ActionVerificationSpecification,
    EvidenceProbeSpecification,
    ProgressMetadataSpecification,
)


@dataclass(frozen=True)
class RepairAdapterRegistration:
    adapter_id: str
    curator_rule: str
    finding_type: str
    executable: bool
    structural: bool = False
    preview_enabled: bool | None = None
    supervised_apply_available: bool = False

    @property
    def can_preview(self) -> bool:
        return self.executable if self.preview_enabled is None else self.preview_enabled


class CuratorRepairAdapterRegistry:
    """Read-only, code-owned repair capability and evidence-specification lookup."""

    DEFAULT_REGISTRATIONS = (
        RepairAdapterRegistration(
            "canonical_article_link",
            "CUR-REL-ARTICLE-CANDIDATE",
            "article_candidate",
            executable=True,
            structural=False,
        ),
        RepairAdapterRegistration(
            "missing_required_upstream_evidence",
            "CUR-WR-TERMINAL-EVIDENCE",
            "workflow_reasoning_evidence_gap",
            executable=False,
            structural=True,
            preview_enabled=True,
            supervised_apply_available=True,
        ),
        RepairAdapterRegistration(
            "missing_post_action_verification",
            "CUR-WR-ACTION-VERIFICATION",
            "workflow_reasoning_unverified_action",
            executable=False,
            structural=True,
            preview_enabled=True,
            supervised_apply_available=False,
        ),
        RepairAdapterRegistration(
            "branch_aware_progress_metadata",
            "CUR-WR-PROGRESS",
            "workflow_reasoning_progress_inconsistency",
            executable=False,
            structural=True,
            preview_enabled=True,
            supervised_apply_available=False,
        ),
    )

    def __init__(self, registrations: Iterable[RepairAdapterRegistration] | None = None,
                 evidence_specs: Iterable[EvidenceProbeSpecification] | None = None,
                 action_verification_specs: Iterable[
                     ActionVerificationSpecification
                 ] | None = None,
                 progress_metadata_specs: Iterable[
                     ProgressMetadataSpecification
                 ] | None = None):
        values = tuple(self.DEFAULT_REGISTRATIONS if registrations is None else registrations)
        self._registrations = {
            (item.curator_rule, item.finding_type): item for item in values
        }
        if evidence_specs is None:
            from app.services.curator_evidence_specification_catalog import (
                PRODUCTION_EVIDENCE_SPECIFICATIONS,
            )
            evidence_specs = PRODUCTION_EVIDENCE_SPECIFICATIONS.all()
        # Only explicitly approved, typed specifications enter the runtime lookup.
        approved_specs = tuple(item for item in evidence_specs if item.approved)
        self._evidence_specs = {
            key: max((item for item in approved_specs if item.evidence_key == key),
                     key=lambda item: (item.version, item.specification_id))
            for key in {item.evidence_key for item in approved_specs}
        }
        if action_verification_specs is None:
            from app.services.curator_action_verification_specification_catalog import (
                PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS,
            )
            action_verification_specs = (
                PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS.all()
            )
        approved_action_specs = tuple(
            item for item in action_verification_specs if item.approved
        )
        self._action_verification_specs = {
            key: max(
                (item for item in approved_action_specs if item.verification_key == key),
                key=lambda item: (item.version, item.specification_id),
            )
            for key in {item.verification_key for item in approved_action_specs}
        }
        if progress_metadata_specs is None:
            from app.services.curator_progress_metadata_specification_catalog import (
                PRODUCTION_PROGRESS_METADATA_SPECIFICATIONS,
            )
            progress_metadata_specs = PRODUCTION_PROGRESS_METADATA_SPECIFICATIONS.all()
        self._progress_metadata_specs = {
            (item.curator_rule, item.finding_type): item
            for item in progress_metadata_specs if item.approved
        }

    def lookup(self, curator_rule: str, finding_type: str) -> RepairAdapterRegistration | None:
        return self._registrations.get((str(curator_rule or ""), str(finding_type or "")))

    def evidence_specification(self, evidence_key: str) -> EvidenceProbeSpecification | None:
        return self._evidence_specs.get(str(evidence_key or ""))

    def action_verification_specification(
        self, verification_key: str,
    ) -> ActionVerificationSpecification | None:
        return self._action_verification_specs.get(str(verification_key or ""))

    def progress_metadata_specification(
        self, curator_rule: str, finding_type: str,
    ) -> ProgressMetadataSpecification | None:
        return self._progress_metadata_specs.get((
            str(curator_rule or ""), str(finding_type or ""),
        ))

    def preview(self, task: dict[str, Any], workflow: dict[str, Any], *,
                workflow_raw_sha256: str = "",
                workflow_semantic_sha256: str = "") -> dict[str, Any]:
        """Dispatch a read-only preview only after registry eligibility succeeds."""
        eligibility = self.eligibility(task)
        if not eligibility["capability_eligible"]:
            return {"available": False, "read_only": True, **eligibility}
        registration = self.lookup(task.get("curator_rule", ""), task.get("finding_type", ""))
        if not registration or not registration.structural:
            return {
                "available": False, "read_only": True, **eligibility,
                "reason": "This adapter retains its existing preview implementation.",
            }
        structural = task["structured_evidence"]
        if registration.adapter_id == "branch_aware_progress_metadata":
            specification = self.progress_metadata_specification(
                task.get("curator_rule", ""), task.get("finding_type", "")
            )
            from app.services.curator_structural_repair_preview_service import (
                CuratorStructuralRepairPreviewService,
            )
            preview = CuratorStructuralRepairPreviewService().preview_progress_metadata(
                task, specification, workflow,
                workflow_raw_sha256=workflow_raw_sha256,
                workflow_semantic_sha256=workflow_semantic_sha256,
            )
            available = bool(preview.get("available"))
            return {
                **eligibility,
                **preview,
                "status": "preview_eligible" if available else "preview_unavailable",
                "preview_eligible": available,
            }
        if registration.adapter_id == "missing_post_action_verification":
            specification = self.action_verification_specification(
                structural.get("verification_key", "")
            )
            from app.services.curator_structural_repair_preview_service import (
                CuratorStructuralRepairPreviewService,
            )
            preview = CuratorStructuralRepairPreviewService().preview(
                task, specification, workflow
            )
            available = bool(preview.get("available"))
            return {
                **eligibility,
                **preview,
                "status": "preview_eligible" if available else "preview_unavailable",
                "preview_eligible": available,
            }
        missing = structural["missing"]
        if len(missing) != 1:
            return {
                "available": False, "read_only": True, **eligibility,
                "preview_eligible": False,
                "reason": "One structural preview currently requires exactly one missing evidence key.",
            }
        specification = self.evidence_specification(missing[0])
        from app.services.curator_structural_repair_preview_service import (
            CuratorStructuralRepairPreviewService,
        )
        preview = CuratorStructuralRepairPreviewService().preview(task, specification, workflow)
        available = bool(preview.get("available"))
        return {
            **eligibility,
            **preview,
            "status": "preview_eligible" if available else "preview_unavailable",
            "preview_eligible": available,
        }

    def eligibility(self, task: dict[str, Any]) -> dict[str, Any]:
        registration = self.lookup(task.get("curator_rule", ""), task.get("finding_type", ""))
        if not registration:
            return self._result("human_review_only", "No registered repair adapter matches this finding.")
        if not registration.can_preview:
            return self._result(
                "registered_adapter_not_previewable",
                "A repair adapter is registered, but read-only preview is not enabled.",
                registration,
            )
        structural = task.get("structured_evidence")
        if registration.adapter_id == "branch_aware_progress_metadata":
            if (task.get("content_type") != "workflow"
                    or not isinstance(structural, dict)
                    or isinstance(structural.get("configured_steps"), bool)
                    or not isinstance(structural.get("configured_steps"), int)
                    or isinstance(structural.get("maximum_user_visible_nodes"), bool)
                    or not isinstance(structural.get("maximum_user_visible_nodes"), int)
                    or structural["configured_steps"] < 1
                    or structural["maximum_user_visible_nodes"] <= structural["configured_steps"]):
                return self._result(
                    "human_review_only",
                    "The finding does not contain one deterministic static progress defect.",
                    registration,
                )
            if not self.progress_metadata_specification(
                    task.get("curator_rule", ""), task.get("finding_type", "")):
                return self._result(
                    "missing_evidence_specification",
                    "No approved progress metadata specification matches this finding.",
                    registration,
                )
            return self._result(
                "preview_candidate",
                "An approved one-field branch-aware progress preview is available.",
                registration,
                capability_eligible=True,
            )
        if registration.adapter_id == "missing_post_action_verification":
            if not isinstance(structural, dict):
                return self._result(
                    "human_review_only",
                    "The finding does not contain typed action-edge evidence.",
                    registration,
                )
            verification_key = str(structural.get("verification_key") or "")
            edge = structural.get("outgoing_edge")
            if not verification_key or not isinstance(edge, dict):
                return self._result(
                    "human_review_only",
                    "The finding has no approved unambiguous action-verification identity.",
                    registration,
                )
            if not self.action_verification_specification(verification_key):
                return self._result(
                    "missing_evidence_specification",
                    "No approved action-verification specification exists for: "
                    + verification_key,
                    registration,
                    missing_evidence_specifications=[verification_key],
                )
            return self._result(
                "preview_candidate",
                "A reviewed action-verification specification and exact edge evidence are available; "
                "topology validation is required.",
                registration,
                capability_eligible=True,
            )
        missing = structural.get("missing") if isinstance(structural, dict) else None
        if registration.structural and (not isinstance(missing, list) or not missing):
            return self._result(
                "human_review_only", "The finding does not contain unambiguous structured evidence.", registration
            )
        missing_specs = sorted(
            str(key) for key in missing or [] if not self.evidence_specification(str(key))
        )
        if missing_specs:
            return self._result(
                "missing_evidence_specification",
                "No approved evidence specification exists for: " + ", ".join(missing_specs),
                registration,
                missing_evidence_specifications=missing_specs,
            )
        return self._result(
            "preview_candidate",
            "Registered adapter and approved evidence specifications are available; topology validation is required.",
            registration, capability_eligible=True,
        )

    @staticmethod
    def _result(status: str, reason: str,
                registration: RepairAdapterRegistration | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "capability_eligible": False,
            "preview_eligible": False,
            "adapter_id": registration.adapter_id if registration else None,
            "structural": bool(registration and registration.structural),
            "execution_eligible": bool(registration and registration.executable),
            "supervised_apply_available": bool(
                registration and registration.supervised_apply_available
            ),
            **extra,
        }
