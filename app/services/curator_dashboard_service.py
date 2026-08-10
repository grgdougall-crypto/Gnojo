from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from curator.auditor import CuratorAuditor
from curator.locking import AuditLock
from curator.memory import CuratorMemoryStore
from curator.governance import CuratorGovernancePolicy
from curator.tasks import KnowledgeTaskService

from app.services.curator_task_service import CuratorTaskService
from app.services.curator_task_inventory_service import CuratorTaskInventoryService
from app.services.curator_dashboard_presentation_service import CuratorDashboardPresentationService
from app.services.knowledge_integrity_service import KnowledgeIntegrityService


class CuratorDashboardService:
    """Read Curator operations data and start deterministic audits."""

    def __init__(self, repository_root: Path | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.output_root = self.repository_root / "curation_runs"
        self.memory_root = self.repository_root / "curation_memory"

    def run_audit(self) -> dict[str, Any]:
        state = CuratorMemoryStore(self.memory_root).load()
        CuratorGovernancePolicy.authorize("audit", "write_audit_output", state["controls"])
        with AuditLock(self.repository_root / ".curator-audit.lock"):
            result, location = CuratorAuditor(
                self.repository_root, self.output_root, self.memory_root
            ).audit()
        return {
            "run_id": result.run_id,
            "location": str(location),
            "findings": len(result.findings),
            "defects": sum(item.classification == "defect" for item in result.findings),
        }

    def dashboard(self, *, sort_by: str = "debt", filters: dict[str, str] | None = None) -> dict[str, Any]:
        state = CuratorMemoryStore(self.memory_root).load()
        latest = self._latest_report()
        tasks = list(state.get("tasks", {}).values())
        for task in tasks:
            task.setdefault("execution_mode", KnowledgeTaskService.execution_mode(task))
        keys = {
            "priority": lambda item: ({"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}.get(item.get("priority"), 5), item.get("task_id", "")),
            "recurrence": lambda item: (-int(item.get("times_observed", 0)), item.get("task_id", "")),
            "category": lambda item: (item.get("category", ""), item.get("task_id", "")),
            "platform": lambda item: (item.get("platform", ""), item.get("task_id", "")),
            "owner": lambda item: (item.get("owner", ""), item.get("task_id", "")),
            "status": lambda item: (item.get("status", ""), item.get("task_id", "")),
            "age": lambda item: (item.get("first_seen", ""), item.get("task_id", "")),
            "confidence": lambda item: ({"high": 0, "medium": 1, "low": 2}.get(item.get("confidence"), 3), item.get("task_id", "")),
            "debt": lambda item: (-float(item.get("knowledge_debt_score", 0)), item.get("task_id", "")),
        }
        tasks = sorted(tasks, key=keys.get(sort_by, keys["debt"]))
        inventory = CuratorTaskInventoryService(self.repository_root).filter(tasks, filters or {})
        tasks = inventory["tasks"]
        task_service = CuratorTaskService(self.repository_root)
        task_presentation = CuratorDashboardPresentationService.present(
            tasks, group_tasks=task_service.grouped
        )
        return {
            "has_audit": bool(latest),
            "latest": latest,
            "tasks": tasks,
            "task_groups": task_service.grouped(tasks),
            "task_presentation": task_presentation,
            "curator_status": task_service.status(state),
            "evolution": task_service.evolution(state),
            "sort_by": sort_by,
            "recent_audits": list(reversed(state.get("audits", [])[-10:])),
            "memory_updated_at": state.get("updated_at"),
            "integrity": KnowledgeIntegrityService(self.repository_root).report(),
            "task_inventory": inventory,
        }

    def _latest_report(self) -> dict[str, Any]:
        if not self.output_root.exists():
            return {}
        directories = sorted(path for path in self.output_root.iterdir() if path.is_dir())
        if not directories:
            return {}
        latest = directories[-1]
        result = self._read_json(latest / "audit_results.json")
        if not result:
            return {}
        result["report_directory"] = str(latest)
        return result

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
