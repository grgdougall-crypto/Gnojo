from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from app.data_root import resolve_application_root, resolve_data_root
from typing import Any
from uuid import uuid4

from curator.inventory import CuratorInventory
from app.services.content_quality_service import ContentQualityService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


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
    "weak_learning_coverage": "learning_content",
    "missing_command_reference": "command_reference",
}

COMMAND_HANDOFF_IDENTITY_FIELDS = (
    "gap_identity",
    "workflow_id",
    "workflow_filename",
    "workflow_lifecycle",
    "node_id",
    "article_id",
    "command_identity",
)


class KnowledgeCoveragePlannerError(ValueError):
    pass


class KnowledgeCoveragePlannerService:
    """Persistent deterministic planning over Gnojo's read-only content inventory."""

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 taxonomy_path: Path | None = None):
        self.repository_root = resolve_data_root(repository_root, legacy_root=Path(__file__).resolve().parents[2])
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.taxonomy_path = taxonomy_path or (
            resolve_application_root(repository_root) / "app" / "data" / "knowledge_coverage_taxonomy.json"
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
        domains = deepcopy(self.taxonomy()["domains"])
        for domain in domains:
            if domain.get("capability_catalog"):
                catalog = self.capability_catalog(domain["id"])
                domain["areas"] = deepcopy(catalog["capabilities"])
                domain["capability_catalog_id"] = catalog["catalog_id"]
                domain["capability_catalog_version"] = catalog["schema_version"]
        return domains

    def capability_catalog(self, domain_id: str) -> dict[str, Any]:
        """Load one immutable, code-owned capability catalog fail-closed."""
        domains = self.taxonomy()["domains"]
        matches = [item for item in domains if item.get("id") == domain_id]
        if len(matches) != 1 or not matches[0].get("capability_catalog"):
            raise KnowledgeCoveragePlannerError(
                f"Coverage domain '{domain_id}' has no capability catalog."
            )
        filename = str(matches[0]["capability_catalog"] or "").strip()
        if not filename or Path(filename).name != filename:
            raise KnowledgeCoveragePlannerError("Capability catalog path is invalid.")
        path = self.taxonomy_path.parent / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeCoveragePlannerError(
                f"Unable to read capability catalog: {error}"
            ) from error
        capabilities = value.get("capabilities")
        if (
            value.get("schema_version") != "1.0"
            or value.get("domain_id") != domain_id
            or not str(value.get("catalog_id") or "").strip()
            or not isinstance(capabilities, list)
            or not capabilities
        ):
            raise KnowledgeCoveragePlannerError("Unsupported capability catalog.")
        identifiers = set()
        allowed_artifacts = {"workflow", "article", "command_reference"}
        allowed_relationships = {"workflow_article", "article_command_reference"}
        for capability in capabilities:
            expected = capability.get("expected_artifacts")
            matches_by_type = capability.get("artifact_matches")
            identifier = str(capability.get("id") or "").strip()
            if (
                not identifier
                or identifier in identifiers
                or not str(capability.get("title") or "").strip()
                or capability.get("level") not in {"core", "optional", "advanced"}
                or not str(capability.get("platform") or "").strip()
                or not str(capability.get("category") or "").strip()
                or not isinstance(capability.get("terms"), list)
                or not capability.get("terms")
                or not isinstance(expected, list)
                or not expected
                or len(expected) != len(set(expected))
                or not set(expected) <= allowed_artifacts
                or not isinstance(capability.get("likely_relationships"), list)
                or not set(capability.get("likely_relationships") or []) <= allowed_relationships
                or len(capability.get("likely_relationships") or []) != len(set(
                    capability.get("likely_relationships") or []
                ))
                or not isinstance(matches_by_type, dict)
                or set(matches_by_type) != {"workflow", "article", "command"}
                or not all(isinstance(matches_by_type[key], list)
                           for key in matches_by_type)
                or any(
                    len(matches_by_type[key]) != len(set(matches_by_type[key]))
                    for key in matches_by_type
                )
            ):
                raise KnowledgeCoveragePlannerError(
                    "Capability catalog contains an invalid or ambiguous capability."
                )
            identifiers.add(identifier)
        return deepcopy(value)

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

    def reconcile_command_reference_handoff(
        self,
        campaign_id: str,
        work_item_id: str,
        binding: dict[str, Any],
        *,
        relationship_handoff: dict[str, Any] | None,
        expected_fingerprint: str,
        actor: str,
    ) -> dict[str, Any]:
        """Bind one legacy command-review work item to current canonical identities."""
        if set(binding) != set(COMMAND_HANDOFF_IDENTITY_FIELDS):
            raise KnowledgeCoveragePlannerError(
                "Command relationship handoff identity is incomplete."
            )
        normalized = {key: str(binding.get(key) or "").strip()
                      for key in COMMAND_HANDOFF_IDENTITY_FIELDS}
        if not all(normalized.values()):
            raise KnowledgeCoveragePlannerError(
                "Command relationship handoff identity is incomplete."
            )

        campaign = self.get(campaign_id)
        if not expected_fingerprint or self._fingerprint(campaign) != expected_fingerprint:
            raise KnowledgeCoveragePlannerError(
                "Coverage campaign changed before command handoff reconciliation."
            )
        metadata = campaign.get("creation_metadata")
        selected = metadata.get("selected_gap") if isinstance(metadata, dict) else None
        if (
            not isinstance(selected, dict)
            or metadata.get("initiated_by") != "autonomous_growth_stage2"
            or selected.get("gap_type") != "missing_command_reference"
        ):
            raise KnowledgeCoveragePlannerError(
                "Coverage campaign does not have authoritative Stage 2 command-gap provenance."
            )

        gaps = [gap for gap in campaign.get("gaps") or [] if (
            isinstance(gap, dict)
            and gap.get("gap_type") == "missing_command_reference"
            and gap.get("area_id") == normalized["workflow_id"]
        )]
        if len(gaps) != 1:
            raise KnowledgeCoveragePlannerError(
                "Command relationship campaign gap identity is ambiguous."
            )
        gap = gaps[0]
        work_items = [item for item in campaign.get("work_items") or [] if (
            isinstance(item, dict)
            and item.get("work_item_id") == work_item_id
            and item.get("gap_id") == gap.get("gap_id")
            and item.get("work_type") == "command_reference"
        )]
        if len(work_items) != 1:
            raise KnowledgeCoveragePlannerError(
                "Command relationship campaign work identity is ambiguous."
            )
        work = work_items[0]

        if relationship_handoff is not None:
            expected_handoff = {
                "schema_version": "1.0",
                "review_item_key": f"command_relationship_review:{work_item_id}",
                "gap_identity": normalized["gap_identity"],
                "workflow_id": normalized["workflow_id"],
                "node_id": normalized["node_id"],
                "article_id": normalized["article_id"],
                "command_identity": normalized["command_identity"],
                "normalized_absent_declarations": sorted(
                    relationship_handoff.get("normalized_absent_declarations") or []
                ),
            }
            allowed_absences = {"article.related_commands", "command.related_articles"}
            if (
                relationship_handoff != expected_handoff
                or not set(expected_handoff["normalized_absent_declarations"]) <= allowed_absences
            ):
                raise KnowledgeCoveragePlannerError(
                    "Command relationship declaration handoff is invalid."
                )

        records = (selected, gap, work)
        for record in records:
            for key, expected in normalized.items():
                current = record.get(key)
                if current not in (None, "") and str(current) != expected:
                    raise KnowledgeCoveragePlannerError(
                        f"Command relationship handoff {key} conflicts with campaign provenance."
                    )
        metadata_identity = metadata.get("gap_identity")
        if metadata_identity not in (None, "") and str(metadata_identity) != normalized["gap_identity"]:
            raise KnowledgeCoveragePlannerError(
                "Command relationship gap identity conflicts with campaign provenance."
            )

        changed = []
        for record_name, record in (("selected_gap", selected), ("gap", gap), ("work_item", work)):
            for key, expected in normalized.items():
                if record.get(key) in (None, ""):
                    record[key] = expected
                    changed.append(f"{record_name}.{key}")
        if metadata.get("gap_identity") in (None, ""):
            metadata["gap_identity"] = normalized["gap_identity"]
            changed.append("creation_metadata.gap_identity")
        current_handoff = work.get("command_relationship_review_handoff")
        if relationship_handoff is not None:
            if current_handoff not in (None, relationship_handoff):
                raise KnowledgeCoveragePlannerError(
                    "Command relationship declaration handoff conflicts with campaign state."
                )
            if current_handoff is None:
                work["command_relationship_review_handoff"] = deepcopy(relationship_handoff)
                changed.append("work_item.command_relationship_review_handoff")
        if not changed:
            return deepcopy(campaign)

        campaign.setdefault("history", []).append({
            "event": "command_relationship_handoff_reconciled",
            "at": self._now(),
            "actor": actor,
            "work_item_id": work_item_id,
            "bound_identity": deepcopy(normalized),
            "changed_fields": changed,
        })
        self._save(campaign)
        return deepcopy(campaign)

    @classmethod
    def campaign_fingerprint(cls, campaign: dict[str, Any]) -> str:
        return cls._fingerprint(campaign)

    @staticmethod
    def command_relationship_handoff(
        work: dict[str, Any], absent_declarations: list[str]
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "review_item_key": f"command_relationship_review:{work.get('work_item_id')}",
            "gap_identity": str(work.get("gap_identity") or ""),
            "workflow_id": str(work.get("workflow_id") or ""),
            "node_id": str(work.get("node_id") or ""),
            "article_id": str(work.get("article_id") or ""),
            "command_identity": str(work.get("command_identity") or ""),
            "normalized_absent_declarations": sorted(absent_declarations),
        }

    def create(self, *, title: str, domain_id: str, objective: str,
               notes: str = "", actor: str = "Human",
               metadata: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "history": [{"event": "created", "at": now, "actor": actor}],
        }
        if metadata:
            campaign["creation_metadata"] = deepcopy(metadata)
        self._save(campaign)
        return deepcopy(campaign)

    def assess_domain(self, domain_id: str) -> dict[str, Any]:
        """Project current coverage without creating or modifying a campaign."""
        domain = self._domain(domain_id)
        records = CuratorInventory(self.repository_root).collect()
        assessment_id = self._stable_id("KCP", "assessment", domain_id)
        assets, areas = self._analyze_areas(domain, records)
        reuse = self._reuse_opportunities(assessment_id, domain, records)
        gaps = self._gaps(assessment_id, domain, areas, reuse)
        return {
            "domain": deepcopy(domain),
            "inventory_count": len(records),
            "assets": assets,
            "areas": areas,
            "reuse_opportunities": reuse,
            "gaps": gaps,
            "capability_summary": self._capability_summary(areas, gaps),
            "fingerprint": self._fingerprint({
                "domain": domain_id, "assets": assets, "areas": areas,
                "reuse": reuse, "gaps": gaps,
            }),
        }

    def assess_stage2_candidates(self) -> list[dict[str, Any]]:
        """Return conservative non-taxonomy Growth candidates without writing state."""
        records = CuratorInventory(self.repository_root).collect()
        lifecycle = CuratorWorkflowLifecycleService(self.repository_root)
        workflow_ids = sorted({
            item.identifier for item in records if item.content_type == "workflow"
        })
        workflows: dict[str, dict[str, Any]] = {}
        provenance: dict[str, dict[str, Any]] = {}
        for workflow_id in workflow_ids:
            if len(lifecycle.drafts(workflow_id)) > 1:
                continue
            target = lifecycle.resolve(workflow_id)
            if not target:
                continue
            workflows[workflow_id] = target.workflow
            provenance[workflow_id] = lifecycle.provenance(target)

        history_path = self.repository_root / "app" / "troubleshooting_history"
        history = (
            TroubleshootingHistoryService(history_path).list(500, environment="production")
            if history_path.exists() else []
        )
        candidates = self._learning_candidates(workflows, provenance, history)
        candidates.extend(self._command_reference_candidates(records, workflows, provenance))
        return sorted(candidates, key=lambda item: item["gap_identity"])

    def analyze(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get(campaign_id)
        domain = self._domain(campaign["domain"])
        records = CuratorInventory(self.repository_root).collect()
        assets, area_results = self._analyze_areas(domain, records)
        reuse = self._reuse_opportunities(campaign_id, domain, records)
        gaps = self._gaps(campaign_id, domain, area_results, reuse)
        seed = self._selected_seed_gap(campaign_id, campaign.get("creation_metadata") or {})
        if seed:
            matching_seed = [
                item for item in gaps
                if item.get("gap_type") == seed.get("gap_type")
                and item.get("gap_identity") == seed.get("gap_identity")
            ]
            if seed.get("capability_id"):
                # A capability-driven autonomous campaign governs one exact
                # selected gap; the full catalog remains a read-only domain
                # projection rather than becoming dozens of unrelated work items.
                gaps = matching_seed or [seed]
            elif not matching_seed:
                gaps.append(seed)
                gaps.sort(key=lambda item: (item["area_id"], item["gap_type"], item["gap_id"]))
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
                "capability_summary": self._capability_summary(area_results, gaps),
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
            commands = [record for record in matched if record.content_type == "command"]
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
            article_ids = {record.identifier for record in articles}
            command_ids = {record.identifier for record in commands}
            article_command_ids = set()
            for record in articles:
                article_command_ids.update(str(value) for value in (
                    record.raw.get("related_commands") or []
                ))
                for reference in record.raw.get("commands") or []:
                    if isinstance(reference, dict):
                        command = str(reference.get("command") or "").strip().casefold()
                        article_command_ids.update(
                            command_id for command_id in command_ids
                            if command == command_id.casefold()
                            or command.startswith(command_id.casefold() + " ")
                        )
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
            expected_artifacts = list(area.get("expected_artifacts") or [])
            expected_relationships = list(area.get("likely_relationships") or [])
            artifact_facets = {
                "workflow": bool(workflows),
                "article": bool(articles),
                "command_reference": bool(commands),
            }
            relationship_facets = {
                "workflow_article": bool(linked_articles.intersection(article_ids)),
                "article_command_reference": bool(
                    article_command_ids.intersection(command_ids)
                ),
            }
            covered_expected = sum(
                artifact_facets[name] for name in expected_artifacts
            )
            covered_relationships = sum(
                relationship_facets.get(name, False) for name in expected_relationships
            )
            required_count = len(expected_artifacts) + len(expected_relationships)
            capability_status = (
                "covered" if required_count and (
                    covered_expected + covered_relationships == required_count
                )
                else "missing" if expected_artifacts and covered_expected == 0
                else "partial"
            )
            results.append({
                "area_id": area["id"], "title": area["title"], "facets": facets,
                "workflow_count": len(workflows), "article_count": len(articles),
                "command_count": len(commands),
                "safety_ambiguous_command_count": sum(
                    self._command_safety_ambiguous(record.raw) for record in commands
                ),
                "linked_article_count": len(linked_articles),
                "relevant_node_count": len(workflow_nodes),
                "coverage_percent": round(sum(facets.values()) * 100 / len(facets)),
                "asset_ids": sorted(record.identifier for record in matched),
                "capability_id": area.get("id") if area.get("expected_artifacts") else None,
                "capability_level": area.get("level"),
                "capability_category": area.get("category"),
                "platform": area.get("platform") or (domain.get("platforms") or [""])[0],
                "domain_id": domain["id"],
                "expected_artifacts": expected_artifacts,
                "likely_relationships": expected_relationships,
                "artifact_facets": artifact_facets,
                "relationship_facets": relationship_facets,
                "capability_status": capability_status,
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
            if area.get("capability_id"):
                for artifact in area.get("expected_artifacts") or []:
                    if not area["artifact_facets"][artifact]:
                        gap_type = {
                            "workflow": "missing_workflow",
                            "article": "missing_article",
                            "command_reference": "missing_command_reference",
                        }[artifact]
                        gaps.append(self._gap(
                            campaign_id, gap_type, area, artifact,
                            evidence=[
                                f"The governed capability catalog expects {artifact.replace('_', ' ')} coverage for {area['title']}.",
                                "No exact allowlisted artifact identity is present in the current inventory.",
                            ],
                        ))
            else:
                for facet, covered in area["facets"].items():
                    if not covered:
                        gaps.append(self._gap(campaign_id, facet_types[facet], area, facet))
                if area["workflow_count"] and area["relevant_node_count"] < 3:
                    gaps.append(self._gap(campaign_id, "shallow_coverage", area, "workflow"))
        for item in reuse:
            area = {"area_id": item["areas"][0], "title": item["areas"][0].replace("-", " ").title()}
            gaps.append(self._gap(campaign_id, "reusable_pattern", area, "relationships_reuse",
                                  evidence=item["evidence"], discriminator=item["opportunity_id"],
                                  reuse_opportunity_id=item["opportunity_id"],
                                  target_asset=item.get("article_id") or item.get("workflow_id")))
        return sorted(gaps, key=lambda item: (item["area_id"], item["gap_type"], item["gap_id"]))

    def _learning_candidates(self, workflows, provenance, history):
        report = ContentQualityService().build(workflows, history)
        candidates = []
        for row in report.get("workflows", []):
            if int(row.get("learning_coverage", 100)) >= 50:
                continue
            workflow_id = row["workflow_id"]
            workflow = workflows[workflow_id]
            missing = self.learning_help_text_candidate_ids(workflow)
            if not missing:
                continue
            identity = f"workflow:{workflow_id}:weak_learning_coverage"
            candidates.append({
                "gap_identity": identity,
                "gap_type": "weak_learning_coverage",
                "title": f"Improve learning guidance for {row['name']}",
                "domain_id": self._domain_for_workflow(workflow),
                "area_id": workflow_id,
                "area_title": row["name"],
                "workflow_id": workflow_id,
                "workflow_filename": provenance[workflow_id]["workflow_filename"],
                "workflow_lifecycle": provenance[workflow_id]["lifecycle"],
                "node_ids": missing,
                "confidence": "high",
                "measurable_deficiency": 100 - int(row["learning_coverage"]),
                "coverage_percent": int(row["learning_coverage"]),
                "evidence_strength": len(missing),
                "runtime_relevance": int(row.get("sessions") or 0),
                "assessment_fingerprint": provenance[workflow_id]["workflow_fingerprint"],
                "evidence": [
                    f"Learning coverage is {row['learning_coverage']}%, below the existing 50% Content Quality threshold.",
                    f"Eligible nodes without help text: {', '.join(missing)}.",
                ],
                "intended_artifact": "learning_content_plan",
                "expected_human_gate": "Workflow Designer learning authoring",
            })
        return candidates

    @classmethod
    def learning_help_text_candidate_ids(cls, workflow: dict[str, Any]) -> list[str]:
        """Return the exact nodes used by weak-learning coverage detection."""
        return [
            node_id
            for node_id, node in sorted((workflow.get("nodes") or {}).items())
            if isinstance(node, dict)
            and node.get("type") in {"question", "instruction"}
            and not str(node.get("help_text") or "").strip()
            and not cls._safety_ambiguous(node)
        ]

    def _command_reference_candidates(self, records, workflows, provenance):
        commands = {
            item.identifier: item.raw for item in records if item.content_type == "command"
        }
        articles = {
            item.identifier: item.raw for item in records
            if item.content_type == "article" and item.state == "published"
        }
        candidates = []
        for workflow_id, workflow in sorted(workflows.items()):
            for node_id, node in sorted((workflow.get("nodes") or {}).items()):
                if not isinstance(node, dict):
                    continue
                article_id = str(node.get("knowledge_article") or "").strip()
                article = articles.get(article_id)
                if not article:
                    continue
                for reference in article.get("commands") or []:
                    if not isinstance(reference, dict):
                        continue
                    command_id = self._structured_command_identity(
                        str(reference.get("command") or ""), commands
                    )
                    if not command_id:
                        continue
                    command = commands[command_id]
                    risk = command.get("risk") or {}
                    if not isinstance(risk, dict) or not risk.get("level"):
                        continue
                    if not self._node_names_command(node, command_id, command):
                        continue
                    reciprocal = (
                        command_id in (article.get("related_commands") or [])
                        and article_id in (command.get("related_articles") or [])
                    )
                    if reciprocal:
                        continue
                    identity = (
                        f"workflow:{workflow_id}:node:{node_id}:"
                        f"missing_command_reference:{command_id}"
                    )
                    relationship_evidence_fingerprint = self._fingerprint({
                        "gap_identity": identity,
                        "workflow_fingerprint": provenance[workflow_id]["workflow_fingerprint"],
                        "article": article,
                        "command": command,
                    })
                    absent_declarations = [
                        label for record, field, label in (
                            (article, "related_commands", "article.related_commands"),
                            (command, "related_articles", "command.related_articles"),
                        ) if field not in record
                    ]
                    candidates.append({
                        "gap_identity": identity,
                        "gap_type": "missing_command_reference",
                        "title": f"Review {command_id} reference support for {workflow.get('name') or workflow_id}",
                        "domain_id": self._domain_for_workflow(workflow),
                        "area_id": workflow_id,
                        "area_title": workflow.get("name") or workflow_id.replace("_", " ").title(),
                        "workflow_id": workflow_id,
                        "workflow_filename": provenance[workflow_id]["workflow_filename"],
                        "workflow_lifecycle": provenance[workflow_id]["lifecycle"],
                        "node_id": node_id,
                        "article_id": article_id,
                        "command_identity": command_id,
                        "command_risk": deepcopy(risk),
                        "confidence": "high",
                        "measurable_deficiency": 1,
                        "evidence_strength": 3,
                        "runtime_relevance": 0,
                        "assessment_fingerprint": provenance[workflow_id]["workflow_fingerprint"],
                        "relationship_evidence_fingerprint": relationship_evidence_fingerprint,
                        "normalized_absent_declarations": absent_declarations,
                        "evidence": [
                            f"Workflow node {workflow_id}:{node_id} links article '{article_id}'.",
                            f"That article contains a structured command reference resolving to '{command_id}'.",
                            "The existing explicit article/command declarations are not reciprocal.",
                        ],
                        "intended_artifact": "command_relationship_review",
                        "expected_human_gate": "Command Library relationship review",
                    })
        unique = {item["gap_identity"]: item for item in candidates}
        return list(unique.values())

    def _selected_seed_gap(self, campaign_id, metadata):
        if metadata.get("initiated_by") not in {
            "autonomous_growth_stage1", "autonomous_growth_stage2"
        }:
            return None
        candidate = metadata.get("selected_gap")
        if not isinstance(candidate, dict) or candidate.get("gap_type") not in {
            "weak_learning_coverage", "missing_command_reference",
            "missing_article", "missing_workflow",
        }:
            return None
        identity = str(candidate.get("gap_identity") or "")
        evidence = candidate.get("evidence")
        if not identity or not isinstance(evidence, list) or not evidence:
            return None
        if candidate["gap_type"] == "weak_learning_coverage" and not (
            candidate.get("workflow_id") and candidate.get("node_ids")
        ):
            return None
        if candidate["gap_type"] == "missing_command_reference" and not all(
            candidate.get(key) for key in ("workflow_id", "node_id", "command_identity")
        ):
            return None
        if candidate["gap_type"] in {"missing_article", "missing_workflow"} and not (
            candidate.get("area_id") and candidate.get("capability_id")
        ):
            return None
        facet = {
            "weak_learning_coverage": "learning",
            "missing_command_reference": "command_reference",
            "missing_article": "article",
            "missing_workflow": "workflow",
        }[candidate["gap_type"]]
        return {
            "gap_id": self._stable_id("KCG", campaign_id, identity),
            "gap_identity": identity,
            "gap_type": candidate["gap_type"],
            "area_id": candidate.get("area_id") or candidate.get("workflow_id"),
            "area_title": candidate.get("area_title") or candidate.get("title"),
            "facet": facet,
            "summary": candidate.get("title"),
            "priority": "medium",
            "confidence": "high",
            "evidence": list(evidence),
            "target_asset": candidate.get("workflow_id"),
            "workflow_id": candidate.get("workflow_id"),
            "workflow_filename": candidate.get("workflow_filename"),
            "workflow_lifecycle": candidate.get("workflow_lifecycle"),
            "node_ids": list(candidate.get("node_ids") or []),
            "node_id": candidate.get("node_id"),
            "article_id": candidate.get("article_id"),
            "command_identity": candidate.get("command_identity"),
            "relationship_evidence_fingerprint": candidate.get(
                "relationship_evidence_fingerprint"
            ),
            "normalized_absent_declarations": list(
                candidate.get("normalized_absent_declarations") or []
            ),
            "capability_id": candidate.get("capability_id"),
            "capability_level": candidate.get("capability_level"),
            "capability_category": candidate.get("capability_category"),
            "platform": candidate.get("platform"),
            "expected_artifacts": list(candidate.get("expected_artifacts") or []),
            "likely_relationships": list(candidate.get("likely_relationships") or []),
        }

    def _gap(self, campaign_id: str, gap_type: str, area: dict[str, Any], facet: str,
             evidence: list[str] | None = None, discriminator: str = "", **relationships) -> dict[str, Any]:
        gap_id = self._stable_id("KCG", campaign_id, area["area_id"], gap_type, discriminator)
        result = {
            "gap_id": gap_id, "gap_type": gap_type, "area_id": area["area_id"],
            "area_title": area["title"], "facet": facet,
            "summary": f"{area['title']} has {gap_type.replace('_', ' ')}.",
            "priority": "medium" if gap_type.startswith("missing_") else "low",
            "confidence": "high", "evidence": evidence or [f"Coverage facet '{facet}' is not present in the current inventory."],
            **relationships,
        }
        if area.get("capability_id"):
            platform = re.sub(
                r"[^a-z0-9]+", "-", str(area.get("platform") or "").casefold()
            ).strip("-")
            result.update({
                "gap_identity": (
                    f"capability:{area['domain_id']}:{platform}:"
                    f"{area['capability_id']}:{gap_type}"
                ),
                "capability_id": area["capability_id"],
                "capability_level": area.get("capability_level"),
                "capability_category": area.get("capability_category"),
                "platform": area.get("platform"),
                "expected_artifacts": list(area.get("expected_artifacts") or []),
                "likely_relationships": list(area.get("likely_relationships") or []),
            })
        return result

    @staticmethod
    def _capability_summary(
        areas: list[dict[str, Any]], gaps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        capabilities = [item for item in areas if item.get("capability_id")]
        counts = {"covered": 0, "partial": 0, "missing": 0}
        categories: dict[str, list[dict[str, Any]]] = {}
        for item in capabilities:
            status = item.get("capability_status") or "partial"
            counts[status] += 1
            categories.setdefault(item.get("capability_category") or "Other", []).append({
                "capability_id": item["capability_id"],
                "title": item["title"],
                "level": item.get("capability_level"),
                "status": status,
                "artifact_facets": deepcopy(item.get("artifact_facets") or {}),
                "relationship_facets": deepcopy(item.get("relationship_facets") or {}),
            })
        gap_counts = {
            name: sum(1 for item in gaps if item.get("gap_type") == name)
            for name in (
                "missing_workflow", "missing_article", "missing_command_reference"
            )
        }
        gap_counts["weak_learning_coverage"] = 0
        return {
            "total": len(capabilities), **counts, "gap_counts": gap_counts,
            "categories": [
                {"name": name, "capabilities": sorted(values, key=lambda row: row["title"])}
                for name, values in sorted(categories.items())
            ],
        }

    def _work_item(self, campaign_id: str, gap: dict[str, Any]) -> dict[str, Any]:
        item = {
            "work_item_id": self._stable_id("KCW", campaign_id, gap["gap_id"]),
            "campaign_id": campaign_id, "gap_id": gap["gap_id"],
            "work_type": GAP_WORK_TYPES[gap["gap_type"]], "area_id": gap["area_id"],
            "target_asset": gap.get("target_asset"), "priority": gap["priority"],
            "reuse_opportunity_id": gap.get("reuse_opportunity_id"),
            "confidence": gap["confidence"], "dependencies": [],
            "evidence": list(gap["evidence"]), "status": "proposed",
        }
        for key in (
            "gap_identity", "workflow_id", "workflow_filename", "workflow_lifecycle",
            "node_ids", "node_id", "article_id", "command_identity",
            "relationship_evidence_fingerprint", "capability_id",
            "capability_level", "capability_category", "platform",
            "expected_artifacts", "likely_relationships",
        ):
            if gap.get(key) not in (None, [], ""):
                item[key] = deepcopy(gap[key])
        if item["work_type"] == "command_reference":
            item["command_relationship_review_handoff"] = (
                self.command_relationship_handoff(
                    item, list(gap.get("normalized_absent_declarations") or [])
                )
            )
        return item

    @staticmethod
    def _structured_command_identity(reference, commands):
        normalized = " ".join(reference.casefold().split())
        if not normalized:
            return None
        matches = []
        for command_id, command in commands.items():
            names = {
                command_id.casefold(),
                str(command.get("name") or "").casefold(),
                str(command.get("syntax") or "").split(" ", 1)[0].casefold(),
            }
            names.discard("")
            if any(normalized == name or normalized.startswith(name + " ") for name in names):
                matches.append(command_id)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _node_names_command(node, command_id, command):
        text = json.dumps(node, sort_keys=True).casefold()
        names = {
            command_id.casefold(),
            str(command.get("name") or "").casefold(),
            str(command.get("syntax") or "").split(" ", 1)[0].casefold(),
        }
        names.discard("")
        return any(
            re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text)
            for name in names
        )

    @staticmethod
    def _safety_ambiguous(node):
        from app.services.knowledge_workflow_generation_service import (
            KnowledgeWorkflowGenerationService,
        )

        text = json.dumps(node, sort_keys=True).casefold()
        return any(word in text for word in KnowledgeWorkflowGenerationService.STATE_CHANGE_WORDS)

    @staticmethod
    def _command_safety_ambiguous(command):
        risk = command.get("risk")
        if not isinstance(risk, dict) or not risk.get("level"):
            return True
        return (
            str(risk.get("level")).casefold() in {"high", "critical"}
            or bool(risk.get("changes_system"))
        )

    def _domain_for_workflow(self, workflow):
        searchable = self._search_text(workflow)
        matches = []
        for domain in self.domains():
            if str(workflow.get("category") or "").casefold() != str(domain.get("category") or "").casefold():
                continue
            if any(self._term_match(searchable, area.get("terms") or []) for area in domain["areas"]):
                matches.append(domain["id"])
        return matches[0] if len(matches) == 1 else ""

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
            areas = [area["id"] for area in domain["areas"] if self._term_match(text, area["terms"])]
            if not areas:
                continue
            opportunities.append({
                "opportunity_id": self._stable_id("KCR", campaign_id, article_id),
                "type": "shared_article", "article_id": article_id,
                "workflow_ids": sorted(workflow_ids), "areas": sorted(areas),
                "evidence": [f"Article '{article_id}' is already linked by {len(workflow_ids)} workflows."],
                "confidence": "high",
            })
        return opportunities

    def _matches_area(self, record: Any, area: dict[str, Any], domain: dict[str, Any]) -> bool:
        exact = area.get("artifact_matches")
        if isinstance(exact, dict):
            key = {"workflow": "workflow", "article": "article", "command": "command"}.get(
                record.content_type
            )
            if not key or record.identifier not in exact.get(key, []):
                return False
            platform = record.platform.casefold()
            return (not platform or "cross-platform" in platform or
                    str(area.get("platform") or "").casefold() in platform)
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
