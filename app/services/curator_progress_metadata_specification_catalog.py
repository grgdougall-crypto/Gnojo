from __future__ import annotations

from app.services.curator_structural_repair_contracts import (
    ProgressMetadataSpecification,
)


BRANCH_AWARE_PROGRESS_SPECIFICATION = ProgressMetadataSpecification.from_dict({
    "specification_id": "branch-aware-progress-metadata-v1",
    "version": 1,
    "curator_rule": "CUR-WR-PROGRESS",
    "finding_type": "workflow_reasoning_progress_inconsistency",
    "metadata_path": "/progress_mode",
    "allowed_before_states": ["absent", "static"],
    "after_value": "branch_aware",
    "approved": True,
    "approved_by": "Gnojo technical review",
    "approved_at": "2026-08-25T00:00:00+00:00",
    "forbidden_mutations": [
        "estimated_steps",
        "nodes",
        "routes",
        "start_node",
        "other_metadata",
        "publication",
        "task_state",
    ],
})


class CuratorProgressMetadataSpecificationCatalog:
    """One immutable, code-owned progress metadata allowlist."""

    def __init__(self):
        self._specification = BRANCH_AWARE_PROGRESS_SPECIFICATION

    def lookup(self, curator_rule: str, finding_type: str):
        specification = self._specification
        if (curator_rule == specification.curator_rule
                and finding_type == specification.finding_type):
            return specification
        return None

    def all(self):
        return (self._specification,)


PRODUCTION_PROGRESS_METADATA_SPECIFICATIONS = (
    CuratorProgressMetadataSpecificationCatalog()
)
