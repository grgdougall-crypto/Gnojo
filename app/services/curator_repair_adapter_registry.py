from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.services.curator_structural_repair_contracts import EvidenceProbeSpecification


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
    )

    def __init__(self, registrations: Iterable[RepairAdapterRegistration] | None = None,
                 evidence_specs: Iterable[EvidenceProbeSpecification] | None = None):
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

    def lookup(self, curator_rule: str, finding_type: str) -> RepairAdapterRegistration | None:
        return self._registrations.get((str(curator_rule or ""), str(finding_type or "")))

    def evidence_specification(self, evidence_key: str) -> EvidenceProbeSpecification | None:
        return self._evidence_specs.get(str(evidence_key or ""))

    def preview(self, task: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
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
        missing = task["structured_evidence"]["missing"]
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
