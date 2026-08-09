from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from curator.memory import CuratorMemoryError, CuratorMemoryStore


class CuratorRepairPlanner:
    """Convert current integrity evidence into a bounded, prioritized repair queue."""

    PRIORITY = {
        "broken_relationship": 10,
        "duplicate_group": 20,
        "inventory_mismatch": 30,
        "orphaned_article": 40,
        "safety_risk": 50,
        "legacy_provenance": 60,
        "editorial_opportunity": 70,
        "recommendation": 80,
    }

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.integrity = KnowledgeIntegrityService(self.root)

    def build(self, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        report = report or self.integrity.report()
        queue: list[dict[str, Any]] = []
        for relation in report["broken_relationships"]:
            queue.append(self._relationship(relation))
        for group in report["duplicate_groups"]:
            queue.append(self._item("duplicate_group", "MERGE_REVIEW", group, priority="High", debt=8,
                                    action="Review this identity group in the existing Merge Workspace.",
                                    confidence=float(group.get("confidence", 0)), effort="Medium", safe=False))
        if report["inventory_mismatches"]:
            queue.append(self._item("inventory_mismatch", "REBUILD_INVENTORY",
                                    {"records": report["inventory_mismatches"]}, priority="High",
                                    debt=4 * len(report["inventory_mismatches"]),
                                    action="Rebuild the generated inventory from canonical published records.",
                                    confidence=100, effort="Low", safe=True))
        for article in report["orphaned_articles"]:
            payload = dict(article)
            payload["classification_reason"] = "No inbound workflow relationship exists, but search-only use cannot be disproved."
            queue.append(self._item("orphaned_article", "AMBIGUOUS", payload, priority="Medium", debt=1,
                                    action="Decide whether to keep this article standalone, link it, or defer.",
                                    confidence=0, effort="Medium", safe=False))
        for article in report["missing_review_metadata"]:
            queue.append(self._item("legacy_provenance", "LEGACY_REVIEW_REQUIRED", article,
                                    priority="Medium", debt=2,
                                    action="Perform a truthful current validation; never invent historical review data.",
                                    confidence=100, effort="Low", safe=False))
        queue.extend(self._knowledge_operations_items(queue))
        queue.sort(key=lambda item: (self.PRIORITY[item["finding_type"]], -item["knowledge_debt"], item["item_id"]))
        for position, item in enumerate(queue, 1):
            item["position"] = position
            item["status"] = "open"
        return queue

    def _knowledge_operations_items(self, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add bounded human-review work already proven by Curator's persistent tasks."""
        try:
            tasks = CuratorMemoryStore(self.root / "curation_memory").load().get("tasks", {})
        except CuratorMemoryError:
            return []
        existing_ids = {
            str(item.get("affected_content", {}).get("id") or "").casefold()
            for item in existing
        }
        results: list[dict[str, Any]] = []
        for task_id, task in tasks.items():
            if task.get("status") in {"resolved", "ignored", "superseded"}:
                continue
            classification = str(task.get("classification") or "")
            finding_type = str(task.get("finding_type") or "")
            identifier = str(task.get("content_identifier") or "")
            # Integrity findings are the authoritative source for these categories.
            if finding_type in {"missing_review_provenance", "malformed_relationship", "orphaned_content"}:
                continue
            if identifier.casefold() in existing_ids and classification == "Defect":
                continue
            if classification == "Risk" and finding_type == "missing_safety_guidance":
                wizard_type, wizard_class, action = (
                    "safety_risk", "SAFETY_GUIDANCE_REVIEW",
                    "Review Curator's proportional safety guidance. No wording changes automatically.",
                )
            elif classification == "Opportunity":
                wizard_type = "editorial_opportunity"
                wizard_class = ("ASSISTED_RESOLUTION_REVIEW" if finding_type == "article_candidate"
                                else "EDITORIAL_REVIEW")
                action = str(task.get("recommended_action") or "Review this reusable knowledge opportunity.")
            elif classification == "Recommendation":
                wizard_type, wizard_class = "recommendation", "SYSTEM_RECOMMENDATION"
                action = str(task.get("recommended_action") or "Review this system-level recommendation.")
            else:
                continue
            evidence = {
                "id": task_id, "task_id": task_id, "title": task.get("title"),
                "finding_type": finding_type, "content_identifier": identifier,
                "content_type": task.get("content_type"), "evidence": task.get("evidence"),
                "current_risk_level": task.get("safety_level"),
                "recommended_safety_level": task.get("recommended_safety_level"),
                "why": task.get("explanation"), "suggested_warning": task.get("suggested_warning"),
                "related_content": task.get("related_content"),
            }
            results.append(self._item(
                wizard_type, wizard_class, evidence,
                priority=str(task.get("priority") or "Medium"),
                debt=int(task.get("knowledge_debt_score") or 0), action=action,
                confidence=self._confidence_percent(task.get("confidence")), effort="Human review", safe=False,
            ))
        return results

    @staticmethod
    def _confidence_percent(value: Any) -> float:
        """Normalize legacy word ratings and current numeric confidence values."""
        if isinstance(value, (int, float)):
            return float(value * 100 if 0 <= value <= 1 else value)
        text = str(value or "").strip().casefold()
        named = {"low": 35.0, "medium": 65.0, "high": 90.0, "exact": 100.0}
        if text in named:
            return named[text]
        try:
            number = float(text.rstrip("%"))
            return number * 100 if 0 <= number <= 1 else number
        except ValueError:
            return 0.0

    def _relationship(self, relation: dict[str, Any]) -> dict[str, Any]:
        current = relation["article"]
        match = self.integrity.identities.resolve_published(current, {"title": current})
        evidence: dict[str, Any] = {"workflow": relation["workflow"], "node": relation["node"],
                                    "source": relation["source"], "current_reference": current}
        if match and match.confidence == 1.0:
            evidence.update({"canonical_target": match.article["id"], "identity_method": match.method,
                             "identity_reasoning": match.reasoning,
                             "impacted_aliases": [key for key, value in self.integrity.identities.aliases().items()
                                                  if value == match.article["id"]],
                             "before": current, "after": match.article["id"]})
            return self._item("broken_relationship", "RELINK_EXISTING", evidence, priority="Critical", debt=10,
                              action="Relink this node to the canonical immutable article ID.", confidence=100,
                              effort="Low", safe=True)
        likely = self._likely_matches(current)
        evidence["likely_matches"] = likely
        if likely and likely[0]["confidence"] >= 75:
            return self._item("broken_relationship", "LIKELY_MATCH_REVIEW", evidence, priority="Critical", debt=10,
                              action="Review likely canonical matches before changing this relationship.",
                              confidence=likely[0]["confidence"], effort="Medium", safe=False)
        return self._item("broken_relationship", "CREATE_ARTICLE_REQUIRED", evidence, priority="Critical", debt=10,
                          action="No suitable canonical article was found; review an existing Assisted Resolution package or create one.",
                          confidence=0, effort="High", safe=False)

    def _likely_matches(self, value: str) -> list[dict[str, Any]]:
        normalized = self.integrity.identities._norm(value)
        scored = []
        for article in self.integrity.repository.get_published():
            ratio = SequenceMatcher(None, normalized, self.integrity.identities._norm(article.get("title"))).ratio()
            if ratio >= .55:
                scored.append({"id": article["id"], "title": article.get("title"),
                               "confidence": round(ratio * 100, 1), "method": "title_similarity"})
        return sorted(scored, key=lambda item: item["confidence"], reverse=True)[:3]

    def _item(self, finding_type: str, classification: str, evidence: dict[str, Any], *,
              priority: str, debt: int, action: str, confidence: float, effort: str, safe: bool) -> dict[str, Any]:
        identity = "|".join(str(evidence.get(key, "")) for key in ("workflow", "node", "article", "id", "key", "current_reference"))
        item_id = "FIX-" + hashlib.sha256(f"{finding_type}|{identity}".encode()).hexdigest()[:12].upper()
        return {"item_id": item_id, "finding_type": finding_type, "classification": classification,
                "priority": priority, "knowledge_debt": debt, "affected_content": evidence,
                "recommended_action": action, "confidence": confidence, "estimated_effort": effort,
                "safe_automatic": safe, "reversible": safe,
                "what_will_change": self._what_changes(classification, evidence),
                "what_will_not_change": "Instructional wording, workflow logic, publication state, and reviewer history will remain unchanged."}

    @staticmethod
    def _what_changes(classification: str, evidence: dict[str, Any]) -> str:
        if classification == "RELINK_EXISTING":
            return f"The knowledge_article value changes from '{evidence['before']}' to '{evidence['after']}'."
        if classification == "REBUILD_INVENTORY":
            return "Only the generated knowledge inventory is rebuilt from canonical published records."
        return "No change is proposed until a reviewer makes the required decision."
