from __future__ import annotations

from typing import Any

from .models import Finding, InventoryRecord


DIMENSIONS = {
    "content_quality": {"content", "taxonomy"},
    "workflow_integrity": {"workflow"},
    "relationship_health": {"workflow"},
    "source_health": {"source"},
    "coverage_health": {"taxonomy", "content"},
    "validation_health": {"application", "test"},
}
RELATIONSHIP_TYPES = {"malformed_relationship", "orphaned_content", "article_candidate", "workflow_convergence_opportunity"}
SEVERITY_PENALTY = {"critical": 20, "high": 14, "medium": 8, "low": 4, "info": 2}


class KnowledgeHealthService:
    def calculate(self, inventory: list[InventoryRecord], findings: list[Finding], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        denominator = max(1, len(inventory))
        scores: dict[str, float] = {}
        for dimension, domains in DIMENSIONS.items():
            relevant = [finding for finding in findings if finding.domain in domains]
            if dimension == "relationship_health":
                relevant = [finding for finding in findings if finding.finding_type in RELATIONSHIP_TYPES]
            penalty = (sum(SEVERITY_PENALTY[finding.severity] for finding in relevant) / (denominator * 20)) * 100
            scores[dimension] = round(max(0.0, 100.0 - penalty), 1)
        overall = round(sum(scores.values()) / len(scores), 1)
        previous_overall = (previous or {}).get("overall_score")
        return {
            "overall_score": overall,
            "previous_overall_score": previous_overall,
            "change": None if previous_overall is None else round(overall - float(previous_overall), 1),
            "trend": "baseline" if previous_overall is None else "improving" if overall > previous_overall else "declining" if overall < previous_overall else "stable",
            "dimensions": scores,
            "inventory_count": len(inventory),
            "finding_count": len(findings),
            "method": "Bounded 0-100 deterministic severity penalties normalized by audited inventory.",
        }
