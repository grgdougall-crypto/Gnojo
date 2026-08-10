from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from app.knowledge.article_validator import ArticleValidator
from app.services.workflow_validation_service import WorkflowValidationService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService

from .models import Finding, InventoryRecord
from .runtime_rules import ActiveRuleRegistry
from .workflow_reasoning import WorkflowReasoningAuditor


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
GENERAL_TEXT = (
    "complete the described action", "note what changes", "diagnostic reasoning",
    "the observed result will determine", "use this step to", "check the issue",
)
SAFETY_LEVELS = (
    (4, ("firmware", "bios", "disk partition", "partitioning", "reset windows", "factory reset", "format disk")),
    (3, ("registry", "regedit", "system restore", "driver rollback", "roll back the driver")),
    (2, ("restart windows", "restart the computer", "restart your computer", "reboot", "restart networking equipment", "restart the router", "restart the modem")),
    (1, ("restart the application", "restart application", "restart the printer", "restart printer", "restart the service")),
)
DEFECT_TYPES = {
    "unreadable_content", "missing_metadata", "workflow_integrity", "unreachable_node",
    "article_validation", "insufficient_source_evidence", "malformed_source",
    "malformed_relationship", "entry_behavior_mismatch", "canonical_identity_mismatch",
    "multiple_published_versions", "missing_review_provenance", "stale_relationship",
}
RISK_TYPES = {
    "overly_general_field", "missing_safety_guidance", "command_quality_gap",
    "script_safety_gap", "duplicate_candidate", "duplicate_knowledge_candidate", "inconsistent_review_state",
}
RECOMMENDATION_TYPES = {
    "inconsistent_taxonomy", "taxonomy_improvement", "coverage_imbalance",
    "metadata_standard_improvement", "review_workflow_improvement",
}
TAXONOMY = {
    "platform": {"windows", "windows 10", "windows 11", "windows server", "macos", "linux", "cross-platform"},
    "category": {"desktop support", "networking", "printers", "printing", "performance", "storage", "applications", "security", "system information", "system integrity", "system services", "diagnostics", "remediation", "administration", "configuration"},
}


class FindingFactory:
    @staticmethod
    def create(*, finding_type: str, severity: str, confidence: str, record: InventoryRecord,
               title: str, explanation: str, evidence: Iterable[str], rule: str,
               action: str, domain: str, future_fix: bool = False,
               classification: str | None = None, safety_level: int | None = None) -> Finding:
        classification = classification or (
            "defect" if finding_type in DEFECT_TYPES else
            "risk" if finding_type in RISK_TYPES else
            "recommendation" if finding_type in RECOMMENDATION_TYPES else "opportunity"
        )
        identity_parts = [classification, finding_type, record.content_type, record.identifier, rule, title]
        if record.content_type in {"workflow", "workflow_node"}:
            # Lifecycle copies may carry materially different deterministic
            # conditions. Preserve their identity instead of deduplicating them
            # before task reconciliation.
            identity_parts.extend((record.state, record.source_path))
        signature = "|".join(identity_parts)
        identifier = "CUR-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12].upper()
        workflow_id, _, node_id = record.identifier.partition(":")
        return Finding(
            identifier=identifier, classification=classification, finding_type=finding_type, severity=severity,
            confidence=confidence, content_type=record.content_type,
            content_identifier=record.identifier, title=title, explanation=explanation,
            evidence=list(evidence), rule=rule, recommended_action=action,
            domain=domain, future_automated_fix=future_fix, safety_level=safety_level,
            provenance={"source_path": record.source_path, "lifecycle": record.state,
                        "workflow_filename": Path(record.source_path).name,
                        "workflow_id": workflow_id if record.content_type in {"workflow", "workflow_node"} else None,
                        "node_id": (node_id if record.content_type == "workflow_node" and node_id else None),
                        "content_fingerprint": CuratorWorkflowLifecycleService.fingerprint(record.raw)},
        )


class CuratorChecks:
    def __init__(self, repository_root: Path | None = None,
                 active_rules: ActiveRuleRegistry | None = None):
        root = (repository_root or Path(__file__).resolve().parents[1]).resolve()
        self.root = root
        self.active_rules = active_rules or ActiveRuleRegistry.from_repository(root)
        self.lifecycle = CuratorWorkflowLifecycleService(root)

    def run_record(self, record: InventoryRecord) -> list[Finding]:
        """Evaluate one persisted record with the canonical Curator rules.

        This deliberately excludes collection-wide duplicate, relationship, coverage,
        and application checks. It is the narrow evaluation boundary used after a
        reviewer edits one workflow; it is not a second rule engine or a full audit.
        """
        findings: list[Finding] = []
        findings.extend(self._metadata(record))
        findings.extend(self._taxonomy(record))
        if record.content_type == "workflow":
            findings.extend(self._workflow(record))
        elif record.content_type == "article":
            findings.extend(self._article(record))
        elif record.content_type == "command":
            findings.extend(self._command(record))
        elif record.content_type == "script":
            findings.extend(self._script(record))
        return findings

    def run(self, inventory: list[InventoryRecord]) -> tuple[list[Finding], dict[str, Any]]:
        findings: list[Finding] = []
        findings.extend(self._duplicates(inventory))
        for record in inventory:
            findings.extend(self.run_record(record))
        findings.extend(self._relationships(inventory))
        findings.extend(self._editorial_intelligence(inventory))
        findings.extend(self._system_recommendations(inventory))
        findings.extend(self._application_invariants(inventory))
        class_order = {"defect": 0, "risk": 1, "opportunity": 2, "recommendation": 3}
        findings = sorted({item.identifier: item for item in findings}.values(), key=lambda item: (class_order[item.classification], SEVERITY_ORDER[item.severity], item.domain, item.content_type, item.content_identifier, item.identifier))
        return findings, self._coverage(inventory)

    def _metadata(self, record: InventoryRecord) -> list[Finding]:
        findings = []
        if record.raw.get("_inventory_error"):
            findings.append(FindingFactory.create(
                finding_type="unreadable_content", severity="high", confidence="high", record=record,
                title="Content file could not be read", explanation=str(record.raw["_inventory_error"]),
                evidence=[record.source_path], rule="CUR-INVENTORY-001",
                action="Repair the JSON file and rerun the audit; do not publish it until it loads cleanly.",
                domain="content",
            ))
        required = {"workflow": ("workflow_id", "name", "category", "platform"),
                    "article": ("id", "title", "category", "overview"),
                    "command": ("id", "title", "category", "summary"),
                    "script": ("id", "name", "category", "summary")}[record.content_type]
        missing = [field for field in required if not self._nonempty(record.raw.get(field))]
        if missing:
            findings.append(FindingFactory.create(
                finding_type="missing_metadata", severity="high", confidence="high", record=record,
                title="Required metadata is missing", explanation="Required fields are absent or empty.",
                evidence=[f"Missing: {', '.join(missing)}", f"Source: {record.source_path}"],
                rule="CUR-META-001", action="Complete the missing fields and rerun validation.",
                domain="content", future_fix=False,
            ))
        embedded_state = str(record.raw.get("status") or (record.raw.get("review") or {}).get("status") or "").casefold()
        state = record.state.casefold()
        compatible = {
            "draft": {"", "draft", "generated draft", "editable copy", "rejected"}, "published": {"", "published", "approved"},
            "archived": {"", "archived"}, "built_in": {"", "built_in", "published", "approved"},
        }
        if state in compatible and embedded_state not in compatible[state]:
            findings.append(FindingFactory.create(
                finding_type="inconsistent_review_state", severity="medium", confidence="high", record=record,
                title="Stored lifecycle and embedded review state differ",
                explanation="Both values may be individually valid, but their combination can confuse reviewers and lifecycle automation.",
                evidence=[f"Store: {record.state}", f"Embedded: {embedded_state}"], rule="CUR-LIFE-001",
                action="Confirm the intended lifecycle state and align the stored metadata through the normal review workflow.",
                domain="content",
            ))
        text_fields = ("overview", "summary", "description", "help_text")
        for field in text_fields:
            value = str(record.raw.get(field) or "").strip()
            if value and (len(value) < 35 or any(term in value.casefold() for term in GENERAL_TEXT)):
                findings.append(FindingFactory.create(
                    finding_type="overly_general_field", severity="medium", confidence="medium", record=record,
                    title=f"{field.replace('_', ' ').title()} may be overly general",
                    explanation="The field is short or matches language that often lacks observable, topic-specific guidance.",
                    evidence=[value[:300]], rule="CUR-CONTENT-001",
                    action="Have a reviewer add specific observations, expected evidence, and scope.",
                    domain="content", future_fix=True,
                ))
        return findings

    def _taxonomy(self, record: InventoryRecord) -> list[Finding]:
        findings = []
        for field, value in (("category", record.category), ("platform", record.platform)):
            values = [part.strip().casefold() for part in value.split(",") if part.strip()]
            suspicious = [item for item in values if item not in TAXONOMY[field]]
            if suspicious:
                findings.append(FindingFactory.create(
                    finding_type="inconsistent_taxonomy", severity="low", confidence="medium", record=record,
                    title=f"Unrecognized {field} value", explanation="The value is outside the current observed Gnojo taxonomy.",
                    evidence=suspicious, rule="CUR-TAX-001", action="Confirm the value and either normalize it or formally add it to the taxonomy.",
                    domain="taxonomy", future_fix=True,
                ))
        return findings

    def _workflow(self, record: InventoryRecord) -> list[Finding]:
        result = WorkflowValidationService().validate(record.raw)
        findings = []
        for message in result["errors"]:
            findings.append(FindingFactory.create(
                finding_type="workflow_integrity", severity="high", confidence="high", record=record,
                title="Workflow validation failed", explanation=message, evidence=[record.source_path],
                rule="GNOJO-WORKFLOW-VALIDATOR", action="Correct the workflow in the designer and validate every route.",
                domain="workflow",
            ))
        for message in result["warnings"]:
            findings.append(FindingFactory.create(
                finding_type="unreachable_node", severity="medium", confidence="high", record=record,
                title="Workflow contains an unreachable node", explanation=message, evidence=[record.source_path],
                rule="GNOJO-WORKFLOW-REACHABILITY", action="Connect the node intentionally or remove it from the draft after review.",
                domain="workflow", future_fix=True,
            ))
        nodes = record.raw.get("nodes", {})
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            text = " ".join(str(node.get(key) or "") for key in ("title", "instruction", "message", "help_text")).casefold()
            safety_level = self._safety_level(text)
            if node.get("type") == "instruction" and safety_level and not self._has_proportional_safety(node, safety_level):
                node_record = self._node_record(record, node_id, node)
                findings.append(FindingFactory.create(
                    finding_type="missing_safety_guidance", severity="high" if safety_level >= 3 else "medium", confidence="high", record=node_record,
                    title=f"Safety level {safety_level} guidance may be incomplete",
                    explanation=self._safety_explanation(safety_level),
                    evidence=[text[:300]], rule=f"CUR-SAFE-L{str(safety_level)}",
                    action="Confirm the action and add only the proportional reminder, save-work, backup, recovery, or administrative guidance it requires.",
                    domain="workflow", future_fix=False, safety_level=safety_level,
                ))
            actionable = self.lifecycle.resolve(record.identifier)
            # Synthetic records used by narrow verification/tests may not exist in
            # a repository store. In that case the supplied record is the only
            # available copy. Real stored records must match the shared resolver.
            is_actionable_copy = not actionable or actionable.source_path == record.source_path
            if (node.get("type") == "instruction"
                    and len(str(node.get("instruction") or "").strip()) > 180
                    and not node.get("knowledge_article") and is_actionable_copy):
                node_record = self._node_record(record, node_id, node)
                findings.append(FindingFactory.create(
                    finding_type="article_candidate", severity="low", confidence="low", record=node_record,
                    title="Instruction may benefit from a supporting article",
                    explanation="The instruction is detailed enough that optional deeper guidance may improve readability. This is a candidate, not an error.",
                    evidence=[str(node.get("instruction"))[:300]], rule="CUR-REL-ARTICLE-CANDIDATE",
                    action="Decide whether concise help text is sufficient or a reviewed article would add value.",
                    domain="content", future_fix=True,
                ))
        actionable = self.lifecycle.resolve(record.identifier)
        is_actionable_copy = not actionable or actionable.source_path == record.source_path
        if is_actionable_copy:
            for observation in WorkflowReasoningAuditor().analyze(record.raw):
                observation_record = (self._node_record(
                    record, observation.node_id, nodes.get(observation.node_id, {}))
                    if observation.node_id else record)
                evidence = list(observation.evidence)
                if observation.structural:
                    evidence.append(f"Structural evidence: {observation.structural}")
                findings.append(FindingFactory.create(
                    finding_type=observation.finding_type,
                    severity=observation.severity,
                    confidence=observation.confidence,
                    record=observation_record,
                    title=observation.title,
                    explanation=observation.explanation,
                    evidence=evidence,
                    rule=observation.rule,
                    action=observation.action,
                    domain="workflow",
                    classification=observation.classification,
                    future_fix=False,
                ))
        return findings

    def _article(self, record: InventoryRecord) -> list[Finding]:
        findings = []
        for error in ArticleValidator.validate(record.raw):
            findings.append(FindingFactory.create(
                finding_type="article_validation", severity="high", confidence="high", record=record,
                title="Article validation failed", explanation=error, evidence=[record.source_path],
                rule="GNOJO-ARTICLE-VALIDATOR", action="Correct the draft through the review workspace before publication.",
                domain="content",
            ))
        sources = record.raw.get("sources")
        if record.state == "published" and not sources:
            findings.append(FindingFactory.create(
                finding_type="insufficient_source_evidence", severity="high", confidence="high", record=record,
                title="Published article has no source evidence", explanation="Published technical guidance should identify authoritative supporting evidence.",
                evidence=[record.source_path], rule="CUR-SOURCE-001", action="Create a revision and attach a verified authoritative source.",
                domain="source",
            ))
        for index, source in enumerate(sources or [], 1):
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            parsed = urlparse(url)
            suspicious = not (parsed.scheme in {"http", "https"} and parsed.netloc) or "|" in url
            if suspicious:
                findings.append(FindingFactory.create(
                    finding_type="malformed_source", severity="high", confidence="high", record=record,
                    title="Source URL is malformed or suspicious", explanation="The stored URL is not a normal HTTP(S) reference or contains a field delimiter.",
                    evidence=[f"Source {index}: {url}"], rule="CUR-SOURCE-002", action="Open the article revision, correct the source entry, and verify the page.",
                    domain="source", future_fix=True,
                ))
        return findings

    def _command(self, record: InventoryRecord) -> list[Finding]:
        findings = []
        required = ("syntax", "examples", "permissions", "risk", "sources", "tags")
        missing = [field for field in required if not self._nonempty(record.raw.get(field))]
        if missing:
            findings.append(FindingFactory.create(
                finding_type="command_quality_gap", severity="medium", confidence="high", record=record,
                title="Command guidance is incomplete", explanation="One or more safety, usage, or provenance sections are empty.",
                evidence=[f"Missing: {', '.join(missing)}"], rule="CUR-CMD-001",
                action="Complete and technically review the command entry.", domain="content",
            ))
        return findings

    def _script(self, record: InventoryRecord) -> list[Finding]:
        required = ["permissions", "risk", "privacy_note", "related_commands"]
        if record.raw.get("kind") == "Automation":
            required.extend(("dry_run", "rollback", "changes"))
        missing = [field for field in required if not self._nonempty(record.raw.get(field))]
        if not missing:
            return []
        return [FindingFactory.create(
            finding_type="script_safety_gap", severity="high" if record.raw.get("kind") == "Automation" else "medium",
            confidence="high", record=record, title="Script safety documentation is incomplete",
            explanation="A reviewed script requires explicit permissions, risk, privacy, and recovery context.",
            evidence=[f"Missing: {', '.join(missing)}"], rule="CUR-SCRIPT-001",
            action="Document and review the missing safety fields before wider use.", domain="content",
        )]

    def _relationships(self, inventory: list[InventoryRecord]) -> list[Finding]:
        findings = []
        by_type = defaultdict(dict)
        article_records = defaultdict(list)
        for item in inventory:
            by_type[item.content_type][item.identifier] = item
            if item.content_type == "article":
                article_records[item.identifier].append(item)
        article_links: Counter[str] = Counter()
        published_article_ids = {
            item.identifier for item in inventory
            if item.content_type == "article" and item.state == "published"
        }
        published_by_title = {
            self._normalized_identity(item.title): item for item in inventory
            if item.content_type == "article" and item.state == "published"
        }
        command_links: Counter[str] = Counter()
        script_links: Counter[str] = Counter()
        for workflow in by_type["workflow"].values():
            for node_id, node in workflow.raw.get("nodes", {}).items():
                if not isinstance(node, dict):
                    continue
                article_id = node.get("knowledge_article")
                if article_id:
                    article_links[str(article_id)] += 1
                    if str(article_id) not in published_article_ids:
                        equivalent = published_by_title.get(self._normalized_identity(article_id))
                        if equivalent:
                            findings.append(FindingFactory.create(
                                finding_type="duplicate_knowledge_candidate", severity="medium", confidence="high", record=workflow,
                                title="Duplicate Knowledge Candidate", explanation="The referenced identifier is absent, but an equivalent published article title exists.",
                                evidence=[str(article_id), equivalent.identifier, equivalent.source_path], rule="CUR-REL-DUPLICATE-001",
                                action=f"Reuse existing article '{equivalent.identifier}' or review it in the merge workspace.", domain="content", future_fix=True,
                            ))
                        else:
                            findings.append(self._missing_link(workflow, node_id, "article", str(article_id)))
        for article in by_type["article"].values():
            for command_id in article.raw.get("related_commands", []):
                command_links[str(command_id)] += 1
                if command_id not in by_type["command"]:
                    findings.append(self._missing_link(article, "related_commands", "command", str(command_id)))
        for script in by_type["script"].values():
            for command_id in script.raw.get("related_commands", []):
                command_links[str(command_id)] += 1
                if command_id not in by_type["command"]:
                    findings.append(self._missing_link(script, "related_commands", "command", str(command_id)))
            for workflow_id in script.raw.get("related_workflows", []):
                script_links[script.identifier] += 1
                if workflow_id not in by_type["workflow"]:
                    findings.append(self._missing_link(script, "related_workflows", "workflow", str(workflow_id)))
        for article in by_type["article"].values():
            if article.state == "published" and not article_links[article.identifier]:
                findings.append(FindingFactory.create(
                    finding_type="orphaned_content", severity="low", confidence="medium", record=article,
                    title="Published article is not linked from an inventoried workflow", explanation="The article may still be useful through search; this is a review candidate, not necessarily an error.",
                    evidence=[article.source_path], rule="CUR-REL-ORPHAN-ARTICLE", action="Confirm whether the article should be linked, retained for search, or archived.",
                    domain="content", future_fix=True,
                ))
            elif article_links[article.identifier] >= 2:
                findings.append(FindingFactory.create(
                    finding_type="multi_workflow_article_opportunity", severity="info", confidence="high", record=article,
                    title="Article supports reusable workflow knowledge",
                    explanation="The article is referenced by multiple workflow nodes. This is healthy reuse and may identify a pattern worth expanding consistently.",
                    evidence=[f"Workflow links: {article_links[article.identifier]}"], rule="CUR-EDITOR-ARTICLE-REUSE-001",
                    action="Confirm the article remains sufficiently general and consider it when reviewing similar unlinked workflow steps.",
                    domain="content", future_fix=False,
                ))
        for canonical, records in article_records.items():
            published = [item for item in records if item.state == "published"]
            if len(published) > 1:
                findings.append(FindingFactory.create(
                    finding_type="multiple_published_versions", severity="critical", confidence="high",
                    record=published[0], title="Multiple published records share one canonical identity",
                    explanation="Only one live published record may own a canonical article identity.",
                    evidence=[item.source_path for item in published], rule="CUR-IDENTITY-002",
                    action="Open the Knowledge Integrity merge workspace and retain one canonical publication.",
                    domain="content", future_fix=True,
                ))
            for item in published:
                review = item.raw.get("review") or {}
                missing = [field for field in ("reviewed_by", "reviewed_at") if not review.get(field)]
                if missing:
                    findings.append(FindingFactory.create(
                        finding_type="missing_review_provenance", severity="high", confidence="high",
                        record=item, title="Published article is missing review provenance",
                        explanation="Published knowledge must record who approved it and when.",
                        evidence=[f"Missing: {', '.join(missing)}", item.source_path], rule="CUR-LIFE-002",
                        action="Create a reviewed revision and record the reviewer and approval timestamp.",
                        domain="content", future_fix=True,
                    ))
        return findings

    def _application_invariants(self, inventory: list[InventoryRecord]) -> list[Finding]:
        if not inventory:
            return []
        from app.services.article_review_service import ArticleReviewService
        probe = "Title | Publisher | https://example.com/reference"
        try:
            parsed = ArticleReviewService._sources(probe)
            valid = parsed == [{"title": "Title | Publisher", "url": "https://example.com/reference"}]
        except Exception:
            valid = False
        if valid:
            return []
        record = InventoryRecord("application", "article-source-parser", "Article source entry", "app/services/article_review_service.py")
        return [FindingFactory.create(
            finding_type="entry_behavior_mismatch", severity="high", confidence="high", record=record,
            title="Article source parser mishandles publisher delimiters", explanation="A source title containing a pipe is not separated from its final URL correctly.",
            evidence=[probe], rule="CUR-APP-SOURCE-001", action="Align the UI preview and backend parser around the final title/URL delimiter.",
            domain="application", future_fix=True,
        )]

    def _editorial_intelligence(self, inventory: list[InventoryRecord]) -> list[Finding]:
        findings: list[Finding] = []
        instruction_groups: dict[str, list[tuple[InventoryRecord, str, dict[str, Any]]]] = defaultdict(list)
        command_tokens = re.compile(r"\b(ipconfig|ping|nslookup|tracert|netstat|sfc|dism|chkdsk|powershell|systeminfo|tasklist)\b", re.I)
        for workflow in (item for item in inventory if item.content_type == "workflow"):
            for node_id, node in workflow.raw.get("nodes", {}).items():
                if not isinstance(node, dict) or node.get("type") != "instruction":
                    continue
                instruction = str(node.get("instruction") or "").strip()
                normalized = re.sub(r"[^a-z0-9]+", " ", instruction.casefold()).strip()
                if len(normalized) >= 45:
                    instruction_groups[normalized].append((workflow, node_id, node))
                matches = sorted({match.casefold() for match in command_tokens.findall(instruction)})
                if matches and not node.get("knowledge_article"):
                    node_record = self._node_record(workflow, node_id, node)
                    findings.append(FindingFactory.create(
                        finding_type="command_reference_candidate", severity="info", confidence="medium", record=node_record,
                        title="Instruction may benefit from a reusable command reference",
                        explanation="The instruction names a command-line diagnostic. A reviewed command asset could centralize syntax, permissions, risk, and examples.",
                        evidence=matches, rule="CUR-EDITOR-COMMAND-001",
                        action="Check the command library for an existing asset; link it or consider creating one after editorial review.",
                        domain="content", future_fix=True,
                    ))
        for occurrences in instruction_groups.values():
            workflows = {item[0].identifier for item in occurrences}
            if len(workflows) < 2:
                continue
            workflow, node_id, node = occurrences[0]
            node_record = self._node_record(workflow, node_id, node)
            findings.append(FindingFactory.create(
                finding_type="reusable_instruction_pattern", severity="info", confidence="high", record=node_record,
                title="Instruction text is repeated across workflows",
                explanation="Equivalent instructional guidance appears in multiple workflows and may be easier to maintain as shared knowledge or a reusable component.",
                evidence=[f"{item[0].identifier}:{item[1]}" for item in occurrences], rule="CUR-EDITOR-REUSE-001",
                action="Decide whether the guidance should remain local, share one article, or become a reusable workflow component.",
                domain="content", future_fix=True,
            ))
        for workflow in (item for item in inventory if item.content_type == "workflow"):
            inbound: Counter[str] = Counter()
            for node in workflow.raw.get("nodes", {}).values():
                if not isinstance(node, dict):
                    continue
                if node.get("next"):
                    inbound[str(node["next"])] += 1
                answers = node.get("answers") or {}
                answer_values = answers.values() if isinstance(answers, dict) else answers if isinstance(answers, list) else []
                for answer in answer_values:
                    if isinstance(answer, dict) and answer.get("next"):
                        inbound[str(answer["next"])] += 1
            convergence = sorted(node for node, count in inbound.items() if count >= 3)
            if convergence:
                findings.append(FindingFactory.create(
                    finding_type="workflow_convergence_opportunity", severity="info", confidence="medium", record=workflow,
                    title="Multiple troubleshooting paths converge",
                    explanation="Several routes intentionally converge on the same nodes. This may be a useful reusable pattern or simplification point, not a defect.",
                    evidence=convergence, rule="CUR-EDITOR-FLOW-001",
                    action="Review whether the convergence is clear to authors and whether shared route components would simplify maintenance.",
                    domain="workflow", future_fix=True,
                ))
        scripted_commands = {
            str(command_id)
            for script in inventory if script.content_type == "script"
            for command_id in script.raw.get("related_commands", [])
        }
        for command in (item for item in inventory if item.content_type == "command"):
            examples = command.raw.get("examples") or []
            if len(examples) >= 3 and command.identifier not in scripted_commands:
                findings.append(FindingFactory.create(
                    finding_type="script_asset_candidate", severity="info", confidence="low", record=command,
                    title="Command family may benefit from a reusable script asset",
                    explanation="The command entry documents several usage patterns but is not referenced by an inventoried script. Automation is optional and must preserve safety and review boundaries.",
                    evidence=[f"Documented examples: {len(examples)}"], rule="CUR-EDITOR-SCRIPT-001",
                    action="Decide whether a parameterized read-only collector or reviewed automation would reduce repetition; do not automate merely to increase asset count.",
                    domain="content", future_fix=True,
                ))
        return findings

    def _system_recommendations(self, inventory: list[InventoryRecord]) -> list[Finding]:
        if not inventory:
            return []
        system = InventoryRecord("application", "gnojo-knowledge-ecosystem", "Gnojo knowledge ecosystem", "curator")
        findings: list[Finding] = []
        workflows = [item for item in inventory if item.content_type == "workflow"]
        platform_counts = Counter(item.platform or "Unspecified" for item in workflows)
        if len(platform_counts) >= 2 and max(platform_counts.values()) >= 3 * max(1, min(platform_counts.values())):
            findings.append(FindingFactory.create(
                finding_type="coverage_imbalance", severity="info", confidence="high", record=system,
                title="Workflow platform coverage is uneven",
                explanation="The inventory has materially different workflow counts across represented platforms. This may reflect product priorities, but it is useful planning evidence.",
                evidence=[f"{key}: {value}" for key, value in sorted(platform_counts.items())], rule="CUR-SYSTEM-COVERAGE-001",
                action="Confirm the intended platform roadmap and prioritize only the gaps that match Gnojo's product strategy.",
                domain="taxonomy", future_fix=False,
            ))
        general_count = sum(1 for item in inventory for value in (item.raw.get("overview"), item.raw.get("summary"), item.raw.get("description"), item.raw.get("help_text")) if isinstance(value, str) and any(term in value.casefold() for term in GENERAL_TEXT))
        if general_count >= 3:
            findings.append(FindingFactory.create(
                finding_type="metadata_standard_improvement", severity="info", confidence="high", record=system,
                title="Content-specific guidance standards could be strengthened",
                explanation="Several records use recurring general-purpose language. A shared editorial standard could improve future generation and review consistency.",
                evidence=[f"General-language fields observed: {general_count}"], rule="CUR-SYSTEM-METADATA-001",
                action="Add examples of observable evidence, scope, prerequisites, and expected outcomes to authoring standards and generator prompts.",
                domain="content", future_fix=False,
            ))
        return findings

    def _duplicates(self, inventory: list[InventoryRecord]) -> list[Finding]:
        findings = []
        groups = defaultdict(list)
        for item in inventory:
            normalized = re.sub(r"[^a-z0-9]+", " ", item.title.casefold()).strip()
            lifecycle = self._lifecycle(item.state)
            groups[(item.content_type, lifecycle, normalized)].append(item)
        for (content_type, lifecycle, _), items in groups.items():
            unique_sources = {(item.identifier, item.source_path) for item in items}
            if len(unique_sources) < 2:
                continue
            record = items[0]
            findings.append(FindingFactory.create(
                finding_type="duplicate_candidate", severity="medium", confidence="medium", record=record,
                title=f"Possible duplicate {content_type} within {lifecycle}", explanation="Multiple records normalize to the same title inside one lifecycle state. Draft, review, published, and archived versions were evaluated separately.",
                evidence=[f"{item.identifier} ({item.source_path})" for item in items], rule="CUR-DUP-001",
                action="Compare the records and retain, merge, version, or archive them intentionally.", domain="content", future_fix=True,
            ))
        published_by_identity = defaultdict(list)
        for item in inventory:
            if item.content_type == "article" and self._lifecycle(item.state) == "published":
                published_by_identity[item.identifier].append(item)
        for identity, items in published_by_identity.items():
            if len(items) < 2:
                continue
            findings.append(FindingFactory.create(
                finding_type="multiple_published_versions", severity="critical", confidence="high",
                record=items[0], title="Canonical identity is published more than once",
                explanation="The live knowledge library contains multiple records for one canonical identity.",
                evidence=[item.source_path for item in items], rule="CUR-IDENTITY-001",
                action="Merge the records and archive superseded copies.", domain="content", future_fix=True,
            ))
        return findings

    @staticmethod
    def _lifecycle(state: str) -> str:
        normalized = state.casefold().strip()
        aliases = {"built_in": "published", "approved": "published", "reviewed": "reviewed", "pending": "pending_review"}
        return aliases.get(normalized, normalized or "unspecified")

    @staticmethod
    def _safety_level(text: str) -> int:
        for level, phrases in SAFETY_LEVELS:
            if any(phrase in text for phrase in phrases):
                return level
        return 0

    def _has_proportional_safety(self, node: dict[str, Any], level: int) -> bool:
        governed = self.active_rules.has_proportional_safety(node, level)
        if governed is not None:
            return governed
        guidance = " ".join(str(node.get(key) or "") for key in ("warning", "prerequisites", "rollback", "help_text", "instruction")).casefold()
        requirements = {
            1: ("wait", "close", "reopen", "brief interruption"),
            2: ("save", "active work", "disconnect", "approval"),
            3: ("backup", "restore point", "rollback", "recovery"),
            4: ("administrator", "administrative", "backup", "approval", "power"),
        }
        return any(term in guidance for term in requirements[level])

    @staticmethod
    def _safety_explanation(level: int) -> str:
        return {
            1: "A low-disruption restart normally needs a brief reminder or expected-impact note.",
            2: "A system or equipment restart may interrupt work or connectivity and should include save-work or interruption guidance.",
            3: "A recovery-sensitive change should identify backup, restore, rollback, or recovery preparation.",
            4: "An administrative or potentially destructive change should identify authorization, backup, power, and recovery requirements.",
        }[level]

    def _missing_link(self, record: InventoryRecord, location: str, target_type: str, target_id: str) -> Finding:
        return FindingFactory.create(
            finding_type="malformed_relationship", severity="high", confidence="high", record=record,
            title=f"Referenced {target_type} does not exist", explanation=f"The relationship at {location} points to an identifier absent from the inventoried {target_type} library.",
            evidence=[target_id, record.source_path], rule="CUR-REL-001",
            action="Correct the identifier, publish the intended target, or remove the stale relationship after review.",
            domain="content", future_fix=True,
        )

    @staticmethod
    def _normalized_identity(value: Any) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())).strip()

    @staticmethod
    def _node_record(workflow: InventoryRecord, node_id: str, node: dict[str, Any]) -> InventoryRecord:
        return InventoryRecord("workflow_node", f"{workflow.identifier}:{node_id}", str(node.get("title") or node.get("question") or node_id), workflow.source_path, workflow.category, workflow.platform, workflow.state, node)

    @staticmethod
    def _nonempty(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return value is not None

    @staticmethod
    def _coverage(inventory: list[InventoryRecord]) -> dict[str, Any]:
        categories = Counter(item.category or "Uncategorized" for item in inventory)
        platforms = Counter(item.platform or "Unspecified" for item in inventory)
        states = Counter(item.state or "Unspecified" for item in inventory)
        return {
            "categories": dict(sorted(categories.items())),
            "platforms": dict(sorted(platforms.items())),
            "states": dict(sorted(states.items())),
        }
