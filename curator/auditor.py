from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .checks import CuratorChecks, SEVERITY_ORDER
from .inventory import CuratorInventory
from .models import AuditFilter, AuditResult
from .memory import CuratorMemoryStore
from .operations import KnowledgeOperationsService
from .reporting import AuditReportWriter


class CuratorAuditor:
    def __init__(self, repository_root: Path | str = ".", output_root: Path | str = "curation_runs", memory_root: Path | str = "curation_memory"):
        self.repository_root = Path(repository_root).resolve()
        output = Path(output_root)
        self.output_root = (output if output.is_absolute() else self.repository_root / output).resolve()
        memory = Path(memory_root)
        self.memory_root = (memory if memory.is_absolute() else self.repository_root / memory).resolve()
        protected = (
            self.repository_root / "app" / "decision_trees",
            self.repository_root / "app" / "workflow_drafts",
            self.repository_root / "app" / "workflow_publications",
            self.repository_root / "knowledge_base",
        )
        if any(target == path.resolve() or path.resolve() in target.parents for target in (self.output_root, self.memory_root) for path in protected):
            raise ValueError("Curator reports cannot be written inside a trusted Gnojo content store.")

    def audit(self, filters: AuditFilter | None = None, *, write: bool = True) -> tuple[AuditResult, Path | None]:
        filters = filters or AuditFilter()
        started = datetime.now(timezone.utc)
        inventory = CuratorInventory(self.repository_root).collect(filters)
        findings, coverage = CuratorChecks().run(inventory)
        if filters.severity:
            threshold = SEVERITY_ORDER.get(filters.severity.casefold())
            if threshold is None:
                raise ValueError(f"Unknown severity: {filters.severity}")
            findings = [item for item in findings if SEVERITY_ORDER[item.severity] <= threshold]
        completed = datetime.now(timezone.utc)
        digest = hashlib.sha256("|".join(item.identifier for item in findings).encode("utf-8")).hexdigest()[:8]
        run_id = f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}"
        result = AuditResult(
            run_id=run_id, auditor_version=__version__, started_at=started.isoformat(),
            completed_at=completed.isoformat(), repository=str(self.repository_root), filters=filters,
            inventory=inventory, findings=findings, coverage=coverage,
        )
        if write:
            KnowledgeOperationsService(CuratorMemoryStore(self.memory_root)).process(result)
        location = AuditReportWriter().write(result, self.output_root) if write else None
        return result, location
