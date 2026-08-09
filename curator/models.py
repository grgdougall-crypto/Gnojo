from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Severity = Literal["critical", "high", "medium", "low", "info"]
Confidence = Literal["high", "medium", "low"]
Classification = Literal["defect", "risk", "opportunity", "recommendation"]
Domain = Literal[
    "content", "workflow", "source", "taxonomy", "application", "test"
]


@dataclass(frozen=True)
class AuditFilter:
    platform: str | None = None
    category: str | None = None
    content_type: str | None = None
    severity: str | None = None
    changed_since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InventoryRecord:
    content_type: str
    identifier: str
    title: str
    source_path: str
    category: str = ""
    platform: str = ""
    state: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        value = asdict(self)
        if not include_raw:
            value.pop("raw", None)
        return value


@dataclass(frozen=True)
class Finding:
    identifier: str
    classification: Classification
    finding_type: str
    severity: Severity
    confidence: Confidence
    content_type: str
    content_identifier: str
    title: str
    explanation: str
    evidence: list[str]
    rule: str
    recommended_action: str
    domain: Domain
    future_automated_fix: bool = False
    safety_level: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditResult:
    run_id: str
    auditor_version: str
    started_at: str
    completed_at: str
    repository: str
    filters: AuditFilter
    inventory: list[InventoryRecord]
    findings: list[Finding]
    coverage: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    knowledge_tasks: dict[str, Any] = field(default_factory=dict)
    knowledge_debt: dict[str, Any] = field(default_factory=dict)
    knowledge_health: dict[str, Any] = field(default_factory=dict)
    lessons_learned: dict[str, Any] = field(default_factory=dict)
    memory_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": {
                "run_id": self.run_id,
                "auditor_version": self.auditor_version,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "repository": self.repository,
                "filters": self.filters.to_dict(),
            },
            "summary": self.summary(),
            "coverage": self.coverage,
            "findings": [item.to_dict() for item in self.findings],
            "knowledge_tasks": self.knowledge_tasks,
            "knowledge_debt": self.knowledge_debt,
            "knowledge_health": self.knowledge_health,
            "lessons_learned": self.lessons_learned,
            "memory_summary": self.memory_summary,
            "errors": list(self.errors),
        }

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        classifications: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
            classifications[finding.classification] = classifications.get(finding.classification, 0) + 1
        inventory_counts: dict[str, int] = {}
        for item in self.inventory:
            inventory_counts[item.content_type] = inventory_counts.get(item.content_type, 0) + 1
        return {
            "inventory": dict(sorted(inventory_counts.items())),
            "findings": len(self.findings),
            "findings_by_severity": counts,
            "findings_by_classification": classifications,
        }
