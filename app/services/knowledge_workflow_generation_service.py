from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_validation_service import WorkflowValidationService
from curator.workflow_reasoning import WorkflowReasoningAuditor


class KnowledgeWorkflowGenerationError(Exception):
    """Raised when governed workflow generation cannot proceed safely."""


class KnowledgeWorkflowGenerationService:
    """Phase 8: deterministic, supervised evidence-to-workflow generation."""

    WORK_TYPES = {
        "workflow", "workflow_branch", "verification_step", "escalation_path",
        "safety_review",
    }
    NODE_TYPES = {"question", "instruction", "resolution", "transition"}
    STATE_CHANGE_WORDS = {
        "restart", "reset", "install", "uninstall", "disable", "enable", "remove",
        "repair", "clear", "flush", "renew", "rollback", "update", "configure",
    }
    SAFETY_WORDS = {
        "save work", "backup", "administrator", "approval", "authorized", "warning",
        "recovery", "restart", "disrupt", "disconnect",
    }

    def __init__(self, repository_root=None, campaign_root=None, drafts_path=None):
        self.repository_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[2]
        self.campaign_root = Path(campaign_root) if campaign_root else self.repository_root / "knowledge_campaigns"
        self.package_root = self.campaign_root / "workflow_generation"
        self.package_root.mkdir(parents=True, exist_ok=True)
        self.drafts = WorkflowDraftService(drafts_path or self.repository_root / "app" / "workflow_drafts")
        self.validator = WorkflowValidationService()
        self.reasoning = WorkflowReasoningAuditor()

    def list_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.package_root.glob("KWG-*.json")):
            value = self._read(path)
            if value.get("campaign_id") == campaign_id:
                values.append(self._live(value))
        return values

    def get(self, generation_id: str) -> dict[str, Any]:
        path = self.package_root / f"{generation_id}.json"
        if not path.is_file():
            raise KnowledgeWorkflowGenerationError("Workflow generation package was not found.")
        return self._live(self._read(path))

    def eligibility(self, campaign_id: str, work_item_id: str) -> dict[str, Any]:
        campaign, work = self._campaign_work(campaign_id, work_item_id)
        reasons = []
        if campaign.get("status") not in {"analyzed", "active", "ready"}:
            reasons.append("Coverage analysis must be current.")
        if work.get("status") != "proposed":
            reasons.append("The work item must remain proposed for human initiation.")
        if work.get("work_type") not in self.WORK_TYPES:
            reasons.append("This work item does not request workflow coverage.")
        if work.get("dependencies"):
            reasons.append("Resolve the work item's dependencies first.")
        claims = self._approved_workflow_claims(campaign_id, work_item_id)
        if not claims:
            reasons.append("Approved current workflow claims are required.")
        elif any(not isinstance(item.get("workflow_spec"), dict) for item in claims):
            reasons.append("Every workflow claim needs an approved structured workflow specification.")
        return {"eligible": not reasons, "reasons": reasons, "campaign": campaign,
                "work_item": work, "approved_claims": claims}

    def prepare(self, campaign_id: str, work_item_id: str, intent: str = "") -> dict[str, Any]:
        gate = self.eligibility(campaign_id, work_item_id)
        if not gate["eligible"]:
            raise KnowledgeWorkflowGenerationError(" ".join(gate["reasons"]))
        work = gate["work_item"]
        canonical = self._resolve_canonical(work)
        inferred = "expand" if canonical else "create"
        intent = str(intent or inferred).strip().lower()
        if intent not in {"create", "expand"}:
            raise KnowledgeWorkflowGenerationError("Choose create or expand intent.")
        if intent == "expand" and not canonical:
            raise KnowledgeWorkflowGenerationError("Expansion requires an unambiguous canonical workflow.")
        if intent == "create" and canonical:
            raise KnowledgeWorkflowGenerationError("Reuse or expand the canonical workflow instead of duplicating it.")
        identity = canonical["workflow_id"] if canonical else self._proposed_identity(work)
        generation_id = self._stable_id("KWG", campaign_id, work_item_id, intent, identity)
        path = self.package_root / f"{generation_id}.json"
        fingerprint = self._input_fingerprint(gate["campaign"], work, gate["approved_claims"], canonical)
        if path.is_file():
            existing = self._read(path)
            if existing.get("fingerprint") == fingerprint:
                return self._live(existing)
            existing.setdefault("revisions", []).append(self._snapshot(existing))
        else:
            existing = {"schema_version": "1.0", "generation_id": generation_id,
                        "created_at": self._now(), "history": [], "revisions": []}
        existing.update({
            "campaign_id": campaign_id,
            "gap_id": work.get("gap_id"),
            "work_item_id": work_item_id,
            "research_package_ids": self._related_ids(campaign_id, work_item_id, "research", "package_id"),
            "evidence_extraction_ids": self._related_ids(campaign_id, work_item_id, "evidence_extraction", "extraction_id"),
            "claim_plan_ids": [item.get("claim_plan_id") for item in gate["approved_claims"] if item.get("claim_plan_id")],
            "approved_claim_ids": [item["claim_id"] for item in gate["approved_claims"]],
            "approved_evidence_ids": sorted({e for item in gate["approved_claims"] for e in item.get("evidence_ids", [])}),
            "intent": intent,
            "target_workflow_id": canonical.get("workflow_id") if canonical else None,
            "target_workflow_state": canonical.get("state") if canonical else None,
            "proposed_workflow_id": identity,
            "status": "prepared",
            "workflow_plan": None,
            "workflow_draft": None,
            "validation_results": [],
            "reasoning_results": [],
            "relationship_results": [],
            "content_studio_filename": None,
            "fingerprint": fingerprint,
            "updated_at": self._now(),
        })
        self._event(existing, "package_prepared", actor="Human")
        self._save(existing)
        return self._live(existing)

    def plan(self, generation_id: str) -> dict[str, Any]:
        package = self.get(generation_id)
        if package.get("workflow_plan") and package.get("status") in {
            "plan_ready", "draft_ready", "approved_for_handoff", "handed_off"
        }:
            return package
        gate = self.eligibility(package["campaign_id"], package["work_item_id"])
        if not gate["eligible"]:
            raise KnowledgeWorkflowGenerationError("The package is no longer eligible: " + " ".join(gate["reasons"]))
        specs = [deepcopy(item["workflow_spec"]) for item in gate["approved_claims"]]
        plan_nodes = []
        seen = set()
        for claim, spec in zip(gate["approved_claims"], specs):
            node_id = str(spec.get("node_id") or "").strip()
            node_type = str(spec.get("type") or "").strip()
            if not node_id or node_type not in self.NODE_TYPES or node_id in seen:
                raise KnowledgeWorkflowGenerationError("Approved workflow claims contain an invalid or duplicate node specification.")
            seen.add(node_id)
            plan_nodes.append({"node_id": node_id, "type": node_type,
                               "operation": spec.get("operation", "add"),
                               "fields": deepcopy(spec.get("fields") or {}),
                               "claim_ids": [claim["claim_id"]],
                               "evidence_ids": list(claim.get("evidence_ids") or []),
                               "source_urls": list(claim.get("source_urls") or [])})
        canonical = self._canonical_by_id(package.get("target_workflow_id"))
        start_node = next((str(item.get("start_node")) for item in specs if item.get("start_node")), "")
        if package["intent"] == "create" and not start_node:
            start_node = plan_nodes[0]["node_id"] if plan_nodes else ""
        plan = {
            "intent": package["intent"], "workflow_id": package["proposed_workflow_id"],
            "name": next((str(item.get("workflow_name")) for item in specs if item.get("workflow_name")),
                         str(gate["work_item"].get("target_asset") or package["proposed_workflow_id"]).replace("_", " ").title()),
            "category": next((str(item.get("category")) for item in specs if item.get("category")),
                             gate["campaign"].get("category", "Troubleshooting")),
            "platform": next((str(item.get("platform")) for item in specs if item.get("platform")),
                             (gate["campaign"].get("platforms") or ["Cross-platform"])[0]),
            "start_node": start_node or (canonical or {}).get("workflow", {}).get("start_node"),
            "nodes": plan_nodes,
            "reuse_decisions": self._reuse_decisions(plan_nodes),
            "expansion_delta": self._expansion_delta(canonical, plan_nodes) if package["intent"] == "expand" else None,
        }
        package["workflow_plan"] = plan
        package["status"] = "plan_ready"
        package["updated_at"] = self._now()
        self._event(package, "workflow_plan_created", actor="System")
        self._save(package)
        return self._live(package)

    def prepare_draft(self, generation_id: str) -> dict[str, Any]:
        package = self.get(generation_id)
        if package.get("workflow_draft") and package.get("status") in {
            "draft_ready", "approved_for_handoff", "handed_off"
        }:
            return package
        if package.get("status") not in {"plan_ready", "needs_revision", "draft_ready"} or not package.get("workflow_plan"):
            raise KnowledgeWorkflowGenerationError("Prepare and review a workflow plan first.")
        workflow = self._assemble(package)
        validation = self._validate(workflow, package)
        package["workflow_draft"] = workflow
        package["validation_results"] = validation["validation"]
        package["reasoning_results"] = validation["reasoning"]
        package["relationship_results"] = validation["relationships"]
        package["status"] = "draft_ready" if validation["valid"] else "needs_revision"
        package["updated_at"] = self._now()
        self._event(package, "workflow_draft_prepared", actor="System", status=package["status"])
        self._save(package)
        return self._live(package)

    def review(self, generation_id: str, decision: str, notes: str = "") -> dict[str, Any]:
        package = self.get(generation_id)
        if decision not in {"approved", "rejected", "needs_revision"}:
            raise KnowledgeWorkflowGenerationError("Unknown review decision.")
        if decision == "approved" and package.get("status") != "draft_ready":
            raise KnowledgeWorkflowGenerationError("Only a valid workflow draft can be approved.")
        package["status"] = {"approved": "approved_for_handoff", "rejected": "rejected",
                             "needs_revision": "needs_revision"}[decision]
        package["review"] = {"decision": decision, "notes": str(notes or "").strip(),
                             "reviewed_at": self._now(), "reviewed_by": "Human"}
        package["updated_at"] = self._now()
        self._event(package, f"workflow_{decision}", actor="Human")
        self._save(package)
        return self._live(package)

    def handoff(self, generation_id: str) -> dict[str, Any]:
        package = self.get(generation_id)
        if package.get("status") == "handed_off" and package.get("content_studio_filename"):
            return package
        if package.get("status") != "approved_for_handoff":
            raise KnowledgeWorkflowGenerationError("Human approval is required before Content Studio handoff.")
        workflow = deepcopy(package["workflow_draft"])
        workflow.setdefault("knowledge_factory", {}).update({
            "generation_id": package["generation_id"], "campaign_id": package["campaign_id"],
            "gap_id": package.get("gap_id"), "work_item_id": package["work_item_id"],
            "claim_ids": package["approved_claim_ids"], "evidence_ids": package["approved_evidence_ids"],
            "intent": package["intent"], "human_reviewed": True,
        })
        filename = self.drafts.save_draft(workflow)
        package["content_studio_filename"] = filename
        package["status"] = "handed_off"
        package["updated_at"] = self._now()
        self._event(package, "content_studio_handoff", actor="Human", filename=filename)
        self._save(package)
        return self._live(package)

    def _assemble(self, package):
        plan = package["workflow_plan"]
        if package["intent"] == "expand":
            canonical = self._canonical_by_id(package["target_workflow_id"])
            if not canonical:
                raise KnowledgeWorkflowGenerationError("The expansion target is no longer available.")
            workflow = deepcopy(canonical["workflow"])
        else:
            workflow = {"workflow_id": plan["workflow_id"], "name": plan["name"],
                        "description": f"Governed workflow for {plan['name']}.",
                        "category": plan["category"], "platform": plan["platform"],
                        "estimated_steps": max(1, len(plan["nodes"])), "start_node": plan["start_node"], "nodes": {}}
        for item in plan["nodes"]:
            node_id, operation = item["node_id"], item.get("operation", "add")
            if operation == "update" and node_id not in workflow["nodes"]:
                raise KnowledgeWorkflowGenerationError(f"Expansion update references missing node '{node_id}'.")
            if operation == "add" and node_id in workflow["nodes"] and package["intent"] == "expand":
                raise KnowledgeWorkflowGenerationError(f"Expansion would overwrite existing node '{node_id}'.")
            node = {"type": item["type"], **deepcopy(item["fields"])}
            node["knowledge_factory"] = {"claim_ids": item["claim_ids"], "evidence_ids": item["evidence_ids"],
                                         "source_urls": item["source_urls"], "generation_id": package["generation_id"]}
            workflow["nodes"][node_id] = node
        return workflow

    def _validate(self, workflow, package):
        core = self.validator.validate(workflow)
        validation = [{"check": "workflow_schema", "level": "error" if not core["is_valid"] else "pass",
                       "messages": core["errors"] + core["warnings"]}]
        if core["unreachable_nodes"]:
            validation.append({"check": "reachability", "level": "error", "messages": core["unreachable_nodes"]})
        provenance_errors = [node_id for node_id, node in workflow.get("nodes", {}).items()
                             if not (node.get("knowledge_factory", {}).get("claim_ids") or
                                     (package["intent"] == "expand" and node_id not in {n["node_id"] for n in package["workflow_plan"]["nodes"]}))]
        validation.append({"check": "provenance", "level": "error" if provenance_errors else "pass",
                           "messages": provenance_errors})
        safety_errors = []
        for node_id, node in workflow.get("nodes", {}).items():
            text = " ".join(str(node.get(key, "")) for key in ("title", "instruction", "help_text")).lower()
            if node.get("type") == "instruction" and any(word in text for word in self.STATE_CHANGE_WORDS):
                if not any(word in text for word in self.SAFETY_WORDS):
                    safety_errors.append(node_id)
        validation.append({"check": "proportional_safety", "level": "error" if safety_errors else "pass",
                           "messages": safety_errors})
        reasoning = [self._observation(item) for item in self.reasoning.analyze(workflow)]
        reasoning_blockers = [item for item in reasoning if item["rule"] in {
            "CUR-WR-ACTION-VERIFICATION", "CUR-WR-TERMINAL-EVIDENCE", "CUR-WR-PROGRESS"
        }]
        relationships = self._validate_relationships(workflow)
        valid = not any(item["level"] == "error" for item in validation) and not reasoning_blockers \
            and not any(item["level"] == "error" for item in relationships)
        return {"valid": valid, "validation": validation, "reasoning": reasoning,
                "relationships": relationships}

    def _validate_relationships(self, workflow):
        workflow_ids = {item["workflow_id"] for item in self._workflow_inventory()}
        article_ids = {path.stem for directory in ("drafts", "published")
                       for path in (self.repository_root / "knowledge_base" / directory).glob("*.json")}
        results = []
        for node_id, node in workflow.get("nodes", {}).items():
            for field, allowed in (("knowledge_article", article_ids), ("next_workflow", workflow_ids)):
                target = str(node.get(field) or "").strip()
                if target:
                    results.append({"node_id": node_id, "field": field, "target": target,
                                    "level": "pass" if target in allowed else "error"})
        return results

    def _reuse_decisions(self, nodes):
        article_ids = {path.stem for directory in ("drafts", "published")
                       for path in (self.repository_root / "knowledge_base" / directory).glob("*.json")}
        results = []
        for item in nodes:
            article = str(item["fields"].get("knowledge_article") or "").strip()
            if article:
                results.append({"node_id": item["node_id"], "asset_type": "article",
                                "asset_id": article, "decision": "reuse" if article in article_ids else "missing"})
        return results

    def _expansion_delta(self, canonical, nodes):
        existing = (canonical or {}).get("workflow", {}).get("nodes", {})
        return {"added": [n["node_id"] for n in nodes if n.get("operation", "add") == "add"],
                "updated": [n["node_id"] for n in nodes if n.get("operation") == "update"],
                "preserved": sorted(set(existing) - {n["node_id"] for n in nodes})}

    def _approved_workflow_claims(self, campaign_id, work_item_id):
        claims = []
        for path in sorted((self.campaign_root / "claim_planning").glob("KCPM-*.json")):
            plan = self._read(path)
            if plan.get("campaign_id") != campaign_id or plan.get("work_item_id") != work_item_id:
                continue
            if plan.get("status") != "ready_for_drafting":
                continue
            for claim in plan.get("claims") or []:
                if claim.get("review_state") == "approved" and not claim.get("stale"):
                    value = deepcopy(claim)
                    value["claim_plan_id"] = plan.get("claim_plan_id")
                    claims.append(value)
        return claims

    def _campaign_work(self, campaign_id, work_item_id):
        path = self.campaign_root / f"{campaign_id}.json"
        if not path.is_file():
            raise KnowledgeWorkflowGenerationError("Coverage campaign was not found.")
        campaign = self._read(path)
        work = next((item for item in campaign.get("work_items") or []
                     if item.get("work_item_id") == work_item_id), None)
        if not work:
            raise KnowledgeWorkflowGenerationError("Coverage work item was not found.")
        return campaign, work

    def _resolve_canonical(self, work):
        target = str(work.get("target_asset") or "").strip().lower()
        area = str(work.get("area_id") or "").strip().lower().replace("-", " ")
        matches = []
        for item in self._workflow_inventory():
            workflow = item["workflow"]
            values = {str(workflow.get("workflow_id", "")).lower(), str(workflow.get("name", "")).lower()}
            if target and (target in values or target.replace(" ", "_") in values):
                matches.append(item)
            elif work.get("work_type") != "workflow" and area and area in " ".join(values).replace("_", " "):
                matches.append(item)
        unique = {item["workflow_id"]: item for item in matches}
        if len(unique) > 1:
            raise KnowledgeWorkflowGenerationError("Canonical workflow identity is ambiguous; human resolution is required.")
        return next(iter(unique.values()), None)

    def _workflow_inventory(self):
        precedence = {"built_in": 1, "published": 2, "draft": 3}
        found = {}
        sources = [
            ("built_in", self.repository_root / "app" / "decision_trees"),
            ("draft", self.repository_root / "app" / "workflow_drafts"),
        ]
        for state, root in sources:
            for path in sorted(root.glob("*.json")):
                try: workflow = self._read(path)
                except KnowledgeWorkflowGenerationError: continue
                workflow_id = str(workflow.get("workflow_id") or path.stem)
                if precedence[state] >= precedence.get(found.get(workflow_id, {}).get("state", ""), 0):
                    found[workflow_id] = {"workflow_id": workflow_id, "state": state, "workflow": workflow}
        publication_root = self.repository_root / "app" / "workflow_publications"
        for manifest in publication_root.glob("*/current.json"):
            try:
                current = self._read(manifest)
                snapshot = self._read(manifest.parent / f"v{int(current['current_version']):04d}.json")
                workflow = snapshot["workflow"]
            except (KnowledgeWorkflowGenerationError, KeyError, ValueError, TypeError):
                continue
            workflow_id = str(workflow.get("workflow_id") or manifest.parent.name)
            if precedence["published"] >= precedence.get(found.get(workflow_id, {}).get("state", ""), 0):
                found[workflow_id] = {"workflow_id": workflow_id, "state": "published", "workflow": workflow}
        return list(found.values())

    def _canonical_by_id(self, workflow_id):
        return next((item for item in self._workflow_inventory() if item["workflow_id"] == workflow_id), None)

    def _proposed_identity(self, work):
        raw = str(work.get("target_asset") or work.get("area_id") or "governed-workflow").lower()
        value = "".join(char if char.isalnum() else "_" for char in raw).strip("_")
        return value or "governed_workflow"

    def _related_ids(self, campaign_id, work_item_id, directory, key):
        values = []
        for path in sorted((self.campaign_root / directory).glob("*.json")):
            item = self._read(path)
            if item.get("campaign_id") == campaign_id and item.get("work_item_id") == work_item_id and item.get(key):
                values.append(item[key])
        return values

    def _live(self, package):
        value = deepcopy(package)
        try:
            gate = self.eligibility(value["campaign_id"], value["work_item_id"])
            current = self._input_fingerprint(gate["campaign"], gate["work_item"], gate["approved_claims"],
                                              self._canonical_by_id(value.get("target_workflow_id")))
            value["stale"] = current != value.get("fingerprint")
            if value["stale"] and value.get("status") not in {"rejected", "handed_off"}:
                value["effective_status"] = "stale"
        except KnowledgeWorkflowGenerationError:
            value["stale"] = True
            value["effective_status"] = "stale"
        return value

    def _input_fingerprint(self, campaign, work, claims, canonical):
        material = {"campaign": campaign.get("last_analyzed_at"), "work": work,
                    "claims": claims, "canonical": (canonical or {}).get("workflow")}
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _stable_id(self, prefix, *parts):
        digest = hashlib.sha256("|".join(str(item) for item in parts).encode()).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    def _snapshot(self, value):
        return {"fingerprint": value.get("fingerprint"), "status": value.get("status"),
                "updated_at": value.get("updated_at"), "workflow_plan": value.get("workflow_plan"),
                "workflow_draft": value.get("workflow_draft")}

    def _observation(self, item):
        return {"rule": item.rule, "classification": item.classification, "node_id": item.node_id,
                "title": item.title, "explanation": item.explanation, "severity": item.severity,
                "confidence": item.confidence, "evidence": list(item.evidence)}

    def _event(self, package, event, actor, **details):
        package.setdefault("history", []).append({"event": event, "actor": actor, "at": self._now(), **details})

    def _save(self, package):
        self._atomic(self.package_root / f"{package['generation_id']}.json", package)

    def _read(self, path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeWorkflowGenerationError("Governed package data could not be read safely.") from error
        if not isinstance(value, dict):
            raise KnowledgeWorkflowGenerationError("Governed package data is invalid.")
        return value

    def _atomic(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
            raise

    def _now(self):
        return datetime.now(timezone.utc).isoformat()
