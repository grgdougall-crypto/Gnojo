from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from curator.inventory import CuratorInventory


CAMPAIGN_STATUSES = (
    "draft", "analyzed", "ready_for_review", "approved_for_build",
    "in_progress", "completed", "archived",
)

GAP_WORK_TYPES = {
    "missing_workflow": "workflow",
    "missing_branch": "workflow_branch",
    "missing_article": "knowledge_article",
    "missing_source": "source_research",
    "missing_verification": "verification_step",
    "missing_escalation": "escalation_path",
    "missing_safety": "safety_review",
    "missing_relationship": "relationship",
    "shallow_coverage": "coverage_review",
    "reusable_pattern": "reuse_review",
    "platform_expansion": "platform_expansion",
    "category_expansion": "category_expansion",
}


class KnowledgeCoveragePlannerError(ValueError):
    pass


class KnowledgeCoveragePlannerService:
    """Persistent deterministic planning over Gnojo's read-only content inventory."""

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 taxonomy_path: Path | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.taxonomy_path = taxonomy_path or (
            self.repository_root / "app" / "data" / "knowledge_coverage_taxonomy.json"
        )

    def taxonomy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.taxonomy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeCoveragePlannerError(f"Unable to read coverage taxonomy: {error}") from error
        if value.get("schema_version") != "1.0" or not isinstance(value.get("domains"), list):
            raise KnowledgeCoveragePlannerError("Unsupported knowledge coverage taxonomy.")
        return value

    def domains(self) -> list[dict[str, Any]]:
        return deepcopy(self.taxonomy()["domains"])

    def list_campaigns(self) -> list[dict[str, Any]]:
        if not self.campaign_root.exists():
            return []
        campaigns = [self._read(path) for path in self.campaign_root.glob("*.json")]
        return sorted(campaigns, key=lambda item: item.get("last_analyzed_at") or item["created_at"], reverse=True)

    def get(self, campaign_id: str) -> dict[str, Any]:
        path = self._path(campaign_id)
        if not path.exists():
            raise KnowledgeCoveragePlannerError(f"Coverage campaign '{campaign_id}' was not found.")
        return self._read(path)

    def create(self, *, title: str, domain_id: str, objective: str,
               notes: str = "") -> dict[str, Any]:
        domain = self._domain(domain_id)
        title, objective = title.strip(), objective.strip()
        if not title or not objective:
            raise KnowledgeCoveragePlannerError("Campaign title and objective are required.")
        now = self._now()
        campaign_id = f"KCP-{uuid4().hex[:12].upper()}"
        campaign = {
            "schema_version": "1.0",
            "campaign_id": campaign_id,
            "title": title,
            "scope": domain["title"],
            "platforms": list(domain["platforms"]),
            "category": domain["category"],
            "domain": domain["id"],
            "objective": objective,
            "status": "draft",
            "created_at": now,
            "last_analyzed_at": None,
            "coverage_snapshot": {},
            "existing_assets": [],
            "gaps": [],
            "reuse_opportunities": [],
            "work_items": [],
            "confidence": "not_analyzed",
            "notes": notes.strip(),
            "history": [{"event": "created", "at": now, "actor": "Human"}],
        }
        self._save(campaign)
        return deepcopy(campaign)

    def analyze(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get(campaign_id)
        domain = self._domain(campaign["domain"])
        records = CuratorInventory(self.repository_root).collect()
        assets, area_results = self._analyze_areas(domain, records)
        reuse = self._reuse_opportunities(campaign_id, domain, records)
        gaps = self._gaps(campaign_id, domain, area_results, reuse)
        work_items = [self._work_item(campaign_id, gap) for gap in gaps]
        fingerprint = self._fingerprint({
            "assets": assets, "areas": area_results, "gaps": gaps,
            "reuse": reuse, "work_items": work_items,
        })
        previous = campaign.get("coverage_snapshot", {}).get("fingerprint")
        now = self._now()
        campaign.update({
            "status": "analyzed" if campaign.get("status") == "draft" else campaign.get("status"),
            "last_analyzed_at": now,
            "coverage_snapshot": {
                "taxonomy_version": self.taxonomy()["schema_version"],
                "inventory_count": len(records),
                "areas": area_results,
                "covered_areas": sum(1 for item in area_results if item["coverage_percent"] == 100),
                "total_areas": len(area_results),
                "fingerprint": fingerprint,
            },
            "existing_assets": assets,
            "gaps": gaps,
            "reuse_opportunities": reuse,
            "work_items": work_items,
            "confidence": self._confidence(area_results),
        })
        if previous != fingerprint:
            campaign.setdefault("history", []).append({
                "event": "analyzed", "at": now, "actor": "Coverage Planner",
                "fingerprint": fingerprint, "gap_count": len(gaps),
            })
        self._save(campaign)
        return deepcopy(campaign)

    def _analyze_areas(self, domain: dict[str, Any], records: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assets: dict[tuple[str, str], dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        for area in domain["areas"]:
            matched = [record for record in records if self._matches_area(record, area, domain)]
            workflows = [record for record in matched if record.content_type == "workflow"]
            articles = [record for record in matched if record.content_type == "article"]
            for record in matched:
                assets[(record.content_type, record.identifier)] = {
                    "content_type": record.content_type, "identifier": record.identifier,
                    "title": record.title, "state": record.state,
                    "category": record.category, "platform": record.platform,
                    "source_path": record.source_path, "areas": sorted(set(
                        assets.get((record.content_type, record.identifier), {}).get("areas", []) + [area["id"]]
                    )),
                }
            workflow_nodes = [node for record in workflows for node in (record.raw.get("nodes") or {}).values()
                              if isinstance(node, dict)]
            linked_articles = {str(node.get("knowledge_article")) for node in workflow_nodes
                               if node.get("knowledge_article")}
            facets = {
                "workflow": bool(workflows),
                "article": bool(articles),
                "provenance_source": any(self._has_sources(record.raw) for record in articles),
                "verification": any(node.get("type") == "question" for node in workflow_nodes),
                "escalation": any(node.get("type") == "transition" or node.get("next_workflow")
                                  for node in workflow_nodes),
                "safety_authorization": self._safety_covered(workflow_nodes),
                "relationships_reuse": bool(linked_articles),
            }
            results.append({
                "area_id": area["id"], "title": area["title"], "facets": facets,
                "workflow_count": len(workflows), "article_count": len(articles),
                "linked_article_count": len(linked_articles),
                "relevant_node_count": len(workflow_nodes),
                "coverage_percent": round(sum(facets.values()) * 100 / len(facets)),
                "asset_ids": sorted(record.identifier for record in matched),
            })
        return sorted(assets.values(), key=lambda item: (item["content_type"], item["identifier"])), results

    def _gaps(self, campaign_id: str, domain: dict[str, Any], areas: list[dict[str, Any]],
              reuse: list[dict[str, Any]]) -> list[dict[str, Any]]:
        gaps: list[dict[str, Any]] = []
        facet_types = {
            "workflow": "missing_workflow", "article": "missing_article",
            "provenance_source": "missing_source", "verification": "missing_verification",
            "escalation": "missing_escalation", "safety_authorization": "missing_safety",
            "relationships_reuse": "missing_relationship",
        }
        for area in areas:
            for facet, covered in area["facets"].items():
                if not covered:
                    gaps.append(self._gap(campaign_id, facet_types[facet], area, facet))
            if area["workflow_count"] and area["relevant_node_count"] < 3:
                gaps.append(self._gap(campaign_id, "shallow_coverage", area, "workflow"))
        for item in reuse:
            area = {"area_id": item["areas"][0], "title": item["areas"][0].replace("-", " ").title()}
            gaps.append(self._gap(campaign_id, "reusable_pattern", area, "relationships_reuse",
                                  evidence=item["evidence"]))
        return sorted(gaps, key=lambda item: (item["area_id"], item["gap_type"], item["gap_id"]))

    def _gap(self, campaign_id: str, gap_type: str, area: dict[str, Any], facet: str,
             evidence: list[str] | None = None) -> dict[str, Any]:
        gap_id = self._stable_id("KCG", campaign_id, area["area_id"], gap_type)
        return {
            "gap_id": gap_id, "gap_type": gap_type, "area_id": area["area_id"],
            "area_title": area["title"], "facet": facet,
            "summary": f"{area['title']} has {gap_type.replace('_', ' ')}.",
            "priority": "medium" if gap_type.startswith("missing_") else "low",
            "confidence": "high", "evidence": evidence or [f"Coverage facet '{facet}' is not present in the current inventory."],
        }

    def _work_item(self, campaign_id: str, gap: dict[str, Any]) -> dict[str, Any]:
        return {
            "work_item_id": self._stable_id("KCW", campaign_id, gap["gap_id"]),
            "campaign_id": campaign_id, "gap_id": gap["gap_id"],
            "work_type": GAP_WORK_TYPES[gap["gap_type"]], "area_id": gap["area_id"],
            "target_asset": None, "priority": gap["priority"],
            "confidence": gap["confidence"], "dependencies": [],
            "evidence": list(gap["evidence"]), "status": "proposed",
        }

    def _reuse_opportunities(self, campaign_id: str, domain: dict[str, Any], records: list[Any]) -> list[dict[str, Any]]:
        relationships: dict[str, set[str]] = {}
        for record in records:
            if record.content_type != "workflow":
                continue
            for node in (record.raw.get("nodes") or {}).values():
                if isinstance(node, dict) and node.get("knowledge_article"):
                    relationships.setdefault(str(node["knowledge_article"]), set()).add(record.identifier)
        opportunities = []
        for article_id, workflow_ids in sorted(relationships.items()):
            if len(workflow_ids) < 2:
                continue
            article = next((item for item in records if item.content_type == "article" and item.identifier == article_id), None)
            text = self._search_text(article.raw) if article else article_id
            areas = [area["id"] for area in domain["areas"] if self._term_match(text, area["terms"])] or [domain["areas"][0]["id"]]
            opportunities.append({
                "opportunity_id": self._stable_id("KCR", campaign_id, article_id),
                "type": "shared_article", "article_id": article_id,
                "workflow_ids": sorted(workflow_ids), "areas": sorted(areas),
                "evidence": [f"Article '{article_id}' is already linked by {len(workflow_ids)} workflows."],
                "confidence": "high",
            })
        return opportunities

    def _matches_area(self, record: Any, area: dict[str, Any], domain: dict[str, Any]) -> bool:
        text = self._search_text(record.raw)
        if not self._term_match(text, area["terms"]):
            return False
        platform = record.platform.casefold()
        return (not platform or "cross-platform" in platform or
                any(value.casefold() in platform for value in domain["platforms"]))

    @staticmethod
    def _search_text(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True).casefold()

    @staticmethod
    def _term_match(text: str, terms: list[str]) -> bool:
        return any(term.casefold() in text for term in terms)

    @staticmethod
    def _has_sources(article: dict[str, Any]) -> bool:
        return any(isinstance(item, dict) and str(item.get("url") or "").startswith(("http://", "https://"))
                   for item in article.get("sources") or [])

    @staticmethod
    def _safety_covered(nodes: list[dict[str, Any]]) -> bool:
        disruptive = ("restart", "reset", "remove", "disable", "update", "install", "uninstall", "firmware", "bios")
        relevant = [node for node in nodes if any(term in json.dumps(node).casefold() for term in disruptive)]
        if not relevant:
            return True
        guidance = ("save", "backup", "authorized", "approval", "warning", "do not", "only if")
        return all(any(term in json.dumps(node).casefold() for term in guidance) for node in relevant)

    def _domain(self, domain_id: str) -> dict[str, Any]:
        domain = next((item for item in self.domains() if item.get("id") == domain_id), None)
        if not domain:
            raise KnowledgeCoveragePlannerError(f"Unknown coverage domain '{domain_id}'.")
        return domain

    def _path(self, campaign_id: str) -> Path:
        if not campaign_id.startswith("KCP-") or not campaign_id[4:].isalnum():
            raise KnowledgeCoveragePlannerError("Invalid coverage campaign ID.")
        return self.campaign_root / f"{campaign_id}.json"

    def _save(self, campaign: dict[str, Any]) -> None:
        self.campaign_root.mkdir(parents=True, exist_ok=True)
        path = self._path(campaign["campaign_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(campaign, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeCoveragePlannerError(f"Unable to read campaign '{path.stem}': {error}") from error
        return value

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _confidence(areas: list[dict[str, Any]]) -> str:
        if not areas:
            return "low"
        return "high" if any(item["asset_ids"] for item in areas) else "medium"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
