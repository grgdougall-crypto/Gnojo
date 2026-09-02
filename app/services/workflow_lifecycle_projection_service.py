from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
    StructuralRepairApplicationRepositoryError,
)
from app.repositories.structural_repair_recovery_repository import (
    StructuralRepairRecoveryRepository,
    StructuralRepairRecoveryRepositoryError,
)
from app.repositories.workflow_publication_review_repository import (
    ACCEPT_FOR_PUBLICATION,
    WorkflowPublicationReviewRepository,
    WorkflowPublicationReviewRepositoryError,
)
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.workflow_runtime_compatibility_service import runtime_overlay_present
from app.services.workflow_validation_service import WorkflowValidationService
from curator.workflow_reasoning import WorkflowReasoningAuditor
from curator.checks import FindingFactory
from curator.models import InventoryRecord


MATCHES_PUBLISHED = "MATCHES_PUBLISHED"
GOVERNED_CHANGES = "GOVERNED_CHANGES"
AUTHORED_OR_UNATTRIBUTED_CHANGES = "AUTHORED_OR_UNATTRIBUTED_CHANGES"
MIXED_CHANGES = "MIXED_CHANGES"
AMBIGUOUS_STATE = "AMBIGUOUS_STATE"
NO_ACTIVE_PUBLICATION = "NO_ACTIVE_PUBLICATION"

READY_FOR_PUBLICATION_REVIEW = "READY_FOR_PUBLICATION_REVIEW"
NOT_READY = "NOT_READY"
NO_UNPUBLISHED_CHANGES = "NO_UNPUBLISHED_CHANGES"


@dataclass(frozen=True)
class SemanticDeltaOperation:
    operation: str
    path: str
    before_summary: str
    after_summary: str
    before_fingerprint: str
    after_fingerprint: str
    provenance: str = "authored_or_unattributed"
    application_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowValidationProjection:
    schema_valid: bool
    graph_valid: bool
    quality_status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    quality_findings: tuple[str, ...]
    reasoning_findings: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowRuntimeProjection:
    selected_version: int | None
    matches_active_publication: bool
    runtime_overlay_present: bool


@dataclass(frozen=True)
class WorkflowReasoningReviewProjection:
    finding_id: str
    rule: str
    rule_label: str
    finding_type: str
    node_id: str
    content_identifier: str
    title: str
    explanation: str
    evidence: tuple[str, ...]
    review_status: str
    review_id: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    note: str = ""


@dataclass(frozen=True)
class WorkflowLifecycleProjection:
    lifecycle_state: str
    publication_review_state: str
    workflow_id: str
    draft_filename: str
    draft_path: str
    draft_raw_fingerprint: str
    draft_semantic_fingerprint: str
    active_published_version: int | None
    published_semantic_fingerprint: str
    runtime: WorkflowRuntimeProjection
    semantic_delta: tuple[SemanticDeltaOperation, ...]
    governed_delta_summary: tuple[str, ...]
    authored_or_unattributed_delta_summary: tuple[str, ...]
    validation: WorkflowValidationProjection
    readiness_reasons: tuple[str, ...]
    ambiguity_reasons: tuple[str, ...]
    evaluated_at: str
    reasoning_reviews: tuple[WorkflowReasoningReviewProjection, ...] = ()
    reasoning_review_error: str = ""


@dataclass(frozen=True)
class _DraftSnapshot:
    filename: str
    path: Path
    content: bytes
    workflow: dict[str, Any]
    raw_fingerprint: str
    semantic_fingerprint: str


@dataclass(frozen=True)
class _PublicationSnapshot:
    version: int
    workflow: dict[str, Any]
    semantic_fingerprint: str
    manifest_bytes: bytes
    version_bytes: bytes


@dataclass(frozen=True)
class _ApplicationTransition:
    application_id: str
    before: str
    after: str
    governed_paths: tuple[str, ...]
    summary: str
    recovered: bool


class WorkflowLifecycleProjectionService:
    """Project draft, publication, runtime, validation, and repair provenance.

    The service has no writer dependency. Draft and publication bytes are
    re-read after evaluation so concurrent changes fail closed without creating
    lock files or other artifacts.
    """

    PROGRESS_RULES = frozenset({
        "PREMATURE_STATIC_PROGRESS", "STATIC_PATH_LENGTH_CONFLICT",
        "BRANCH_PROGRESS_INTEGRITY", "UNKNOWN_PROGRESS_MODE",
    })
    GRAPH_ERROR_RULES = frozenset({
        "MISSING_BRANCH_DESTINATION", "TERMINAL_OUTGOING_BRANCH",
        "CYCLE_DETECTED", "NONTERMINATING_PATH", "BROKEN_WORKFLOW_HANDOFF",
    })

    def __init__(
        self,
        repository_root: Path,
        *,
        application_repository: Any | None = None,
        recovery_repository: Any | None = None,
        publication_review_repository: Any | None = None,
        runtime_selector: Callable[[str, int, dict[str, Any]], WorkflowRuntimeProjection] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.root = Path(repository_root).resolve()
        curator_root = self.root / "curation_memory"
        self.applications = application_repository or StructuralRepairApplicationRepository(curator_root)
        self.recoveries = recovery_repository or StructuralRepairRecoveryRepository(curator_root)
        self.publication_reviews = (
            publication_review_repository
            or WorkflowPublicationReviewRepository(curator_root)
        )
        self.runtime_selector = runtime_selector or self._runtime_selection
        self.now = now or (lambda: datetime.now(timezone.utc))

    def project(self, workflow_id: str) -> WorkflowLifecycleProjection:
        workflow_id = str(workflow_id or "").strip()
        ambiguity: list[str] = []
        drafts = self._drafts(workflow_id, ambiguity)
        publication = self._publication(workflow_id, ambiguity)
        draft = drafts[0] if len(drafts) == 1 else None
        if len(drafts) != 1:
            ambiguity.append(
                "Exactly one authoritative editable draft is required."
                if not drafts else "Multiple editable drafts claim this workflow identity."
            )

        workflow = draft.workflow if draft else {}
        validation = self._validation(workflow) if draft else self._empty_validation()
        reasoning_reviews, reasoning_review_error = (
            self._reasoning_reviews(draft) if draft else ((), "")
        )
        delta = self._semantic_delta(publication.workflow, workflow) if publication and draft else ()
        if publication and draft and delta:
            lifecycle, delta, governed, authored, provenance_ambiguity = self._provenance(
                workflow_id, publication, draft, delta
            )
            ambiguity.extend(provenance_ambiguity)
        else:
            lifecycle = MATCHES_PUBLISHED if publication and draft else NO_ACTIVE_PUBLICATION
            governed, authored = (), ()

        runtime = (
            self.runtime_selector(workflow_id, publication.version, publication.workflow)
            if publication else WorkflowRuntimeProjection(None, False, False)
        )
        if publication and not runtime.matches_active_publication:
            ambiguity.append("Runtime selection does not match the active publication manifest.")
        if draft and not self._draft_unchanged(draft):
            ambiguity.append("Editable draft changed during lifecycle evaluation.")
        if publication and not self._publication_unchanged(workflow_id, publication):
            ambiguity.append("Active publication changed during lifecycle evaluation.")

        if ambiguity:
            lifecycle = AMBIGUOUS_STATE
        elif not publication:
            lifecycle = NO_ACTIVE_PUBLICATION
        elif not delta:
            lifecycle = MATCHES_PUBLISHED

        reasons = self._readiness_reasons(
            lifecycle, publication, draft, delta, validation, runtime, ambiguity,
            reasoning_reviews, reasoning_review_error,
        )
        review_state = (
            NO_UNPUBLISHED_CHANGES if lifecycle == MATCHES_PUBLISHED
            else NOT_READY if reasons else READY_FOR_PUBLICATION_REVIEW
        )
        return WorkflowLifecycleProjection(
            lifecycle, review_state, workflow_id,
            draft.filename if draft else "", self._relative(draft.path) if draft else "",
            draft.raw_fingerprint if draft else "",
            draft.semantic_fingerprint if draft else "",
            publication.version if publication else None,
            publication.semantic_fingerprint if publication else "", runtime, delta,
            governed, authored, validation, tuple(reasons),
            tuple(dict.fromkeys(ambiguity)), self.now().isoformat(),
            reasoning_reviews, reasoning_review_error,
        )

    def _reasoning_reviews(
        self, draft: _DraftSnapshot,
    ) -> tuple[tuple[WorkflowReasoningReviewProjection, ...], str]:
        workflow = draft.workflow
        workflow_id = str(workflow.get("workflow_id") or "")
        source_path = self._relative(draft.path)
        record = InventoryRecord(
            "workflow", workflow_id,
            str(workflow.get("name") or workflow.get("title") or workflow_id),
            source_path, str(workflow.get("category") or ""),
            self._platform(workflow), "draft", workflow,
        )
        findings = []
        for observation in WorkflowReasoningAuditor().analyze(workflow):
            node_id = str(observation.node_id or "")
            node = workflow.get("nodes", {}).get(node_id, {}) if node_id else {}
            affected = (
                InventoryRecord(
                    "workflow_node", f"{workflow_id}:{node_id}",
                    str(node.get("title") or node.get("question") or node_id),
                    source_path, record.category, record.platform, "draft", node,
                )
                if node_id else record
            )
            findings.append((observation, FindingFactory.create(
                finding_type=observation.finding_type,
                severity=observation.severity,
                confidence=observation.confidence,
                record=affected,
                title=observation.title,
                explanation=observation.explanation,
                evidence=observation.evidence,
                rule=observation.rule,
                action=observation.action,
                domain="workflow",
                classification=observation.classification,
                structured_evidence=observation.structural,
            )))
        try:
            stored = self.publication_reviews.list_for_workflow(workflow_id)
        except WorkflowPublicationReviewRepositoryError as error:
            return tuple(self._pending_review(item, finding) for item, finding in findings), str(error)

        projected = []
        for observation, finding in findings:
            identity = (
                finding.identifier, observation.rule, observation.finding_type,
                finding.content_identifier, observation.node_id,
            )
            related = [item for item in stored if (
                item.finding_id, item.rule, item.finding_type,
                item.content_identifier, item.node_id,
            ) == identity]
            exact = [item for item in related if (
                item.draft_semantic_fingerprint == draft.semantic_fingerprint
                and item.disposition == ACCEPT_FOR_PUBLICATION
            )]
            if len(exact) > 1:
                return tuple(self._pending_review(item, value) for item, value in findings), (
                    f"Multiple publication-review acceptances match finding {finding.identifier}."
                )
            review = exact[0] if exact else None
            status = "accepted" if review else "stale" if related else "pending"
            projected.append(WorkflowReasoningReviewProjection(
                finding.identifier, observation.rule,
                WorkflowReasoningAuditor.RULE_LABELS.get(observation.rule, observation.rule),
                observation.finding_type, observation.node_id,
                finding.content_identifier, observation.title, observation.explanation,
                tuple(observation.evidence), status,
                review.review_id if review else "", review.reviewer if review else "",
                review.reviewed_at if review else "", review.note if review else "",
            ))
        return tuple(projected), ""

    @staticmethod
    def _pending_review(observation: Any, finding: Any) -> WorkflowReasoningReviewProjection:
        return WorkflowReasoningReviewProjection(
            finding.identifier, observation.rule,
            WorkflowReasoningAuditor.RULE_LABELS.get(observation.rule, observation.rule),
            observation.finding_type, observation.node_id,
            finding.content_identifier, observation.title, observation.explanation,
            tuple(observation.evidence), "pending",
        )

    @staticmethod
    def _platform(workflow: dict[str, Any]) -> str:
        value = workflow.get("platform") or workflow.get("platforms") or ""
        return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)

    def _drafts(self, workflow_id: str, ambiguity: list[str]) -> tuple[_DraftSnapshot, ...]:
        directory = self.root / "app" / "workflow_drafts"
        if not directory.is_dir():
            return ()
        found = []
        targets = CuratorWorkflowLifecycleService(self.root).drafts(workflow_id)
        for target in targets:
            path = self.root / target.source_path
            try:
                content = path.read_bytes()
                workflow = json.loads(content.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                ambiguity.append("An authoritative editable draft changed or became unreadable.")
                continue
            if (not isinstance(workflow, dict) or workflow != target.workflow
                    or str(workflow.get("workflow_id") or path.stem) != workflow_id):
                ambiguity.append("An authoritative editable draft changed during resolution.")
                continue
            found.append(_DraftSnapshot(
                path.name, path, content, workflow,
                StructuralRepairFingerprint.raw_workflow(content), self._fingerprint(workflow),
            ))
        canonical_path = directory / f"{workflow_id}.json"
        if canonical_path.is_file() and not any(item.path == canonical_path for item in found):
            ambiguity.append("The canonical editable draft is unreadable or malformed.")
        return tuple(found)

    def _publication(self, workflow_id: str, ambiguity: list[str]) -> _PublicationSnapshot | None:
        directory = self.root / "app" / "workflow_publications" / workflow_id
        manifest_path = directory / "current.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            version = manifest.get("current_version")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError
            version_path = directory / f"v{version:04d}.json"
            version_bytes = version_path.read_bytes()
            snapshot = json.loads(version_bytes.decode("utf-8"))
            workflow = snapshot.get("workflow")
            publication = snapshot.get("publication")
            if (not isinstance(workflow, dict) or not isinstance(publication, dict)
                    or publication.get("version") != version
                    or workflow.get("workflow_id") != workflow_id):
                raise ValueError
            semantic = self._fingerprint(workflow)
            publication_hash = self._publication_content_hash(workflow)
            if (str(publication.get("content_hash") or "") != publication_hash
                    or str(manifest.get("content_hash") or publication_hash)
                    != publication_hash):
                raise ValueError
            return _PublicationSnapshot(version, workflow, semantic, manifest_bytes, version_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            ambiguity.append("The active publication manifest or snapshot is inconsistent.")
            return None

    def _provenance(
        self, workflow_id: str, publication: _PublicationSnapshot,
        draft: _DraftSnapshot, delta: tuple[SemanticDeltaOperation, ...],
    ) -> tuple[str, tuple[SemanticDeltaOperation, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        ambiguity: list[str] = []
        transitions: list[_ApplicationTransition] = []
        incomplete = False
        try:
            for application_id in self.applications.list_application_ids():
                history = self.applications.get(application_id)
                if not history or history[-1].workflow_id != workflow_id:
                    continue
                record = history[-1]
                if record.outcome != "applied" or not record.finalized_at:
                    if record.outcome != "failed":
                        incomplete = True
                    continue
                recovered = any(
                    event.get("outcome") == "recovered"
                    for event in self.recoveries.events(application_id)
                )
                material = self.recoveries.get(application_id)
                original = json.loads(material["original_bytes"].decode("utf-8"))
                if (material.get("application_id", application_id) != application_id
                        or material.get("workflow_id", workflow_id) != workflow_id
                        or material.get("workflow_path", record.workflow_path)
                        != record.workflow_path
                        or material.get("workflow_raw_sha256_before",
                                        record.workflow_raw_sha256_before)
                        != record.workflow_raw_sha256_before
                        or material.get("expected_workflow_raw_sha256_after",
                                        record.expected_workflow_raw_sha256_after)
                        != record.expected_workflow_raw_sha256_after
                        or self._fingerprint(original) != record.workflow_semantic_sha256_before
                        or material.get("expected_workflow_semantic_sha256_after")
                        != record.expected_workflow_semantic_sha256_after):
                    raise ValueError("Recovery provenance does not match its application journal.")
                paths = self._governed_paths(record, original)
                transitions.append(_ApplicationTransition(
                    application_id, record.workflow_semantic_sha256_before,
                    record.expected_workflow_semantic_sha256_after, paths,
                    self._application_summary(record, paths), recovered,
                ))
        except (StructuralRepairApplicationRepositoryError,
                StructuralRepairRecoveryRepositoryError, UnicodeDecodeError,
                json.JSONDecodeError, ValueError, AttributeError) as error:
            return AMBIGUOUS_STATE, delta, (), (), (
                f"Governed repair provenance is unreadable or inconsistent: {error}",
            )

        relevant = [item for item in transitions if item.before in {
            publication.semantic_fingerprint, draft.semantic_fingerprint
        } or item.after in {publication.semantic_fingerprint, draft.semantic_fingerprint}]
        if incomplete:
            ambiguity.append("An incomplete governed application exists for this workflow.")
        if any(item.recovered for item in relevant):
            ambiguity.append("A recovered governed application intersects the current lifecycle state.")

        by_before: dict[str, list[_ApplicationTransition]] = {}
        for item in transitions:
            if not item.recovered:
                by_before.setdefault(item.before, []).append(item)
        chain: list[_ApplicationTransition] = []
        current = publication.semantic_fingerprint
        seen = set()
        while current != draft.semantic_fingerprint:
            candidates = by_before.get(current, [])
            if len(candidates) > 1:
                ambiguity.append("Governed application history branches from the same workflow state.")
                break
            if not candidates:
                break
            item = candidates[0]
            if item.application_id in seen or not item.after:
                ambiguity.append("Governed application history is cyclic or incomplete.")
                break
            chain.append(item)
            seen.add(item.application_id)
            current = item.after

        ending = [item for item in transitions
                  if not item.recovered and item.after == draft.semantic_fingerprint]
        if len(ending) > 1:
            ambiguity.append("Multiple governed applications claim the current draft state.")
        if ambiguity:
            return AMBIGUOUS_STATE, delta, (), (), tuple(ambiguity)
        if chain and current == draft.semantic_fingerprint:
            ids = tuple(item.application_id for item in chain)
            governed = tuple(self._mark(item, "governed", ids) for item in delta)
            return GOVERNED_CHANGES, governed, tuple(item.summary for item in chain), (), ()

        claims = tuple(dict.fromkeys(path for item in ending for path in item.governed_paths))
        claim_ids = tuple(item.application_id for item in ending)
        marked = tuple(
            self._mark(item, "governed", claim_ids) if self._covered(item.path, claims) else item
            for item in delta
        )
        governed_ops = tuple(item for item in marked if item.provenance == "governed")
        authored_ops = tuple(item for item in marked if item.provenance != "governed")
        state = (MIXED_CHANGES if governed_ops and authored_ops else GOVERNED_CHANGES
                 if governed_ops else AUTHORED_OR_UNATTRIBUTED_CHANGES)
        return (
            state, marked,
            tuple(item.summary for item in ending) if governed_ops else (),
            tuple(f"{item.operation} {item.path}" for item in authored_ops), (),
        )

    def _validation(self, workflow: dict[str, Any]) -> WorkflowValidationProjection:
        result = WorkflowValidationService().validate(workflow)
        quality = result.get("quality") or {}
        findings = tuple(
            f"{item.get('severity', '')}:{item.get('rule', '')}:{item.get('node_id') or ''}"
            for item in quality.get("findings", [])
        )
        reasoning = tuple(
            f"{item.rule}:{item.finding_type}:{item.node_id}"
            for item in WorkflowReasoningAuditor().analyze(workflow)
        )
        error_rules = {
            str(item.get("rule") or "") for item in quality.get("findings", [])
            if item.get("severity") == "ERROR"
        }
        graph_valid = bool(
            result.get("is_valid") and not result.get("unreachable_nodes")
            and not (error_rules & self.GRAPH_ERROR_RULES)
        )
        return WorkflowValidationProjection(
            schema_valid=bool(result.get("is_valid")), graph_valid=graph_valid,
            quality_status=str(quality.get("overall_status") or "UNKNOWN"),
            errors=tuple(str(item) for item in result.get("errors", [])),
            warnings=tuple(str(item) for item in result.get("warnings", [])),
            quality_findings=findings, reasoning_findings=reasoning,
        )

    def _readiness_reasons(
        self, lifecycle: str, publication: _PublicationSnapshot | None,
        draft: _DraftSnapshot | None, delta: tuple[SemanticDeltaOperation, ...],
        validation: WorkflowValidationProjection, runtime: WorkflowRuntimeProjection,
        ambiguity: list[str],
        reasoning_reviews: tuple[WorkflowReasoningReviewProjection, ...],
        reasoning_review_error: str,
    ) -> list[str]:
        reasons = list(dict.fromkeys(ambiguity))
        if not publication:
            reasons.append("No active publication exists for comparison.")
        if not draft:
            reasons.append("Exactly one authoritative editable draft is unavailable.")
        if publication and draft and not delta:
            return []
        if lifecycle == AMBIGUOUS_STATE:
            reasons.append("Lifecycle or provenance state is ambiguous.")
        if not validation.schema_valid:
            reasons.append("Workflow schema validation failed.")
        if not validation.graph_valid:
            reasons.append("Workflow graph validation failed.")
        quality_rules = {item.split(":", 2)[1] for item in validation.quality_findings}
        if quality_rules & self.PROGRESS_RULES:
            reasons.append("Workflow progress-integrity validation is not clean.")
        if validation.quality_status == "ERROR":
            reasons.append("Workflow quality validation contains blocking errors.")
        if reasoning_review_error:
            reasons.append(
                "Publication reasoning-review records are malformed or ambiguous."
            )
        elif validation.reasoning_findings and (
            len(reasoning_reviews) != len(validation.reasoning_findings)
            or any(item.review_status != "accepted" for item in reasoning_reviews)
        ):
            reasons.append("Deterministic workflow reasoning findings require review.")
        if publication and not runtime.matches_active_publication:
            reasons.append("Runtime selection is not coherent with the active publication.")
        return list(dict.fromkeys(reasons))

    @classmethod
    def _semantic_delta(
        cls, before: Any, after: Any, path: str = ""
    ) -> tuple[SemanticDeltaOperation, ...]:
        operations: list[SemanticDeltaOperation] = []
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                pointer = f"{path}/{cls._escape(key)}"
                if key not in before:
                    operations.append(cls._operation("add", pointer, None, after[key]))
                elif key not in after:
                    operations.append(cls._operation("remove", pointer, before[key], None))
                else:
                    operations.extend(cls._semantic_delta(before[key], after[key], pointer))
        elif isinstance(before, list) and isinstance(after, list):
            if len(before) == len(after):
                for index, (left, right) in enumerate(zip(before, after)):
                    operations.extend(cls._semantic_delta(left, right, f"{path}/{index}"))
            elif before != after:
                operations.append(cls._operation("replace", path or "/", before, after))
        elif before != after:
            operations.append(cls._operation("replace", path or "/", before, after))
        return tuple(operations)

    @classmethod
    def _operation(cls, operation: str, path: str,
                   before: Any, after: Any) -> SemanticDeltaOperation:
        return SemanticDeltaOperation(
            operation, path, cls._summary(before), cls._summary(after),
            cls._fingerprint(before), cls._fingerprint(after),
        )

    @staticmethod
    def _mark(item: SemanticDeltaOperation, provenance: str,
              application_ids: tuple[str, ...]) -> SemanticDeltaOperation:
        return SemanticDeltaOperation(
            item.operation, item.path, item.before_summary, item.after_summary,
            item.before_fingerprint, item.after_fingerprint, provenance, application_ids,
        )

    @classmethod
    def _governed_paths(cls, record: Any, before: dict[str, Any]) -> tuple[str, ...]:
        paths = [str(item.get("path")) for item in record.metadata_changes]
        paths.extend(f"/nodes/{cls._escape(node_id)}" for node_id in record.proposed_node_ids)
        for edge in record.changed_edges:
            paths.append(cls._edge_path(before, edge))
        return tuple(dict.fromkeys(path for path in paths if path))

    @classmethod
    def _edge_path(cls, workflow: dict[str, Any], edge: Any) -> str:
        base = f"/nodes/{cls._escape(edge.source)}"
        node = workflow.get("nodes", {}).get(edge.source, {})
        if edge.route == "next":
            return f"{base}/next"
        if edge.route in {"skip_to", "conditions not matched"}:
            return f"{base}/skip_to"
        answers = node.get("answers") if isinstance(node, dict) else {}
        if isinstance(answers, dict):
            for key, answer in answers.items():
                target = answer.get("next") if isinstance(answer, dict) else answer
                label = answer.get("label") if isinstance(answer, dict) else key
                if target == edge.destination and edge.route in {str(key), str(label)}:
                    suffix = "/next" if isinstance(answer, dict) else ""
                    return f"{base}/answers/{cls._escape(str(key))}{suffix}"
        return base

    @staticmethod
    def _application_summary(record: Any, paths: tuple[str, ...]) -> str:
        mutation = ", ".join(paths) if paths else "approved workflow mutation"
        return f"{record.application_id}: {mutation}"

    @staticmethod
    def _covered(path: str, claims: tuple[str, ...]) -> bool:
        return any(path == claim or path.startswith(claim + "/") for claim in claims)

    @staticmethod
    def _runtime_selection(
        workflow_id: str, version: int, workflow: dict[str, Any]
    ) -> WorkflowRuntimeProjection:
        return WorkflowRuntimeProjection(
            selected_version=version, matches_active_publication=True,
            runtime_overlay_present=runtime_overlay_present(workflow_id, workflow),
        )

    def _draft_unchanged(self, draft: _DraftSnapshot) -> bool:
        try:
            return draft.path.read_bytes() == draft.content
        except OSError:
            return False

    def _publication_unchanged(
        self, workflow_id: str, publication: _PublicationSnapshot
    ) -> bool:
        directory = self.root / "app" / "workflow_publications" / workflow_id
        try:
            return (
                (directory / "current.json").read_bytes() == publication.manifest_bytes
                and (directory / f"v{publication.version:04d}.json").read_bytes()
                == publication.version_bytes
            )
        except OSError:
            return False

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path.resolve())

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _publication_content_hash(workflow: dict[str, Any]) -> str:
        encoded = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _summary(cls, value: Any) -> str:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return text if len(text) <= 240 else text[:237] + "..."

    @staticmethod
    def _escape(value: str) -> str:
        return str(value).replace("~", "~0").replace("/", "~1")

    @staticmethod
    def _empty_validation() -> WorkflowValidationProjection:
        return WorkflowValidationProjection(False, False, "UNKNOWN", (), (), (), ())
