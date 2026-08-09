from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from app.services.workflow_validation_service import WorkflowValidationService


class CuratorRepairValidator:
    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()

    def relationship(self, item: dict[str, Any]) -> dict[str, Any]:
        evidence = item["affected_content"]
        path = self.root / evidence["source"]
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {"verified": False, "errors": [f"Workflow could not be read: {error}"]}
        errors = WorkflowValidationService().validate(workflow).get("errors", [])
        node = (workflow.get("nodes") or {}).get(evidence["node"], {})
        target = node.get("knowledge_article")
        published_ids = {article["id"] for article in KnowledgeIntegrityService(self.root).repository.get_published()}
        relationship_ok = target == evidence.get("canonical_target") and target in published_ids
        return {"verified": relationship_ok and not errors, "relationship_verified": relationship_ok,
                "workflow_valid": not errors, "errors": errors}

    def inventory(self) -> dict[str, Any]:
        report = KnowledgeIntegrityService(self.root).report()
        return {"verified": not report["inventory_mismatches"], "remaining": report["inventory_mismatches"]}
