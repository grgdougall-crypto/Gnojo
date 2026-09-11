from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from app.services.knowledge_campaign_orchestration_service import (
    ACTION_POLICY,
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_workflow_generation_service import (
    KnowledgeWorkflowGenerationError,
    KnowledgeWorkflowGenerationService,
)
from app.services.workflow_lifecycle_projection_service import (
    WorkflowLifecycleProjectionService,
)
from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)
from app.services.workflow_validation_service import WorkflowValidationService


class KnowledgeBuilderError(ValueError):
    pass


class KnowledgeBuilderService:
    """Three-step supervised facade over the existing governed workflow pipeline."""

    MAX_SAFE_TRANSITIONS = 9
    MAX_EXTERNAL_OPERATIONS = 1

    def __init__(self, orchestration=None, workflows=None, drafts=None,
                 publications=None, lifecycle_factory=None, evidence=None):
        self.orchestration = orchestration or KnowledgeCampaignOrchestrationService()
        self.workflows = workflows or self.orchestration.workflows
        self.evidence = evidence or self.orchestration.evidence
        self.drafts = drafts or self.workflows.drafts
        self.publications = publications or WorkflowPublicationService()
        self.lifecycle_factory = lifecycle_factory or WorkflowLifecycleProjectionService

    def index(self) -> list[dict[str, Any]]:
        values = []
        for record in self.orchestration.read_persisted(
            self.orchestration.campaign_root
        ):
            try:
                campaign = self.orchestration.planner.get(record["campaign_id"])
            except Exception:
                continue
            states = {item.get("work_item_id"): item
                      for item in record.get("work_item_states") or []}
            for work in campaign.get("work_items") or []:
                # Knowledge Builder owns the governed missing-workflow pipeline.
                # Other orchestration types (notably legacy safety_review work)
                # retain their specialist review surfaces and must not appear as
                # interchangeable workflow Builder items.
                if work.get("work_type") != "workflow":
                    continue
                state = states.get(work.get("work_item_id"), {})
                values.append({
                    "campaign_id": campaign["campaign_id"],
                    "work_item_id": work["work_item_id"],
                    "title": state.get("title") or work.get("title")
                    or str(work.get("target_asset") or "Workflow knowledge").replace("_", " ").title(),
                    "state": state.get("state") or "not_started",
                    "next_action": state.get("next_action"),
                })
        return values

    def project(self, campaign_id: str, work_item_id: str) -> dict[str, Any]:
        campaign, work, record, state = self._resolve(campaign_id, work_item_id)
        packages = [item for item in self.workflows.list_for_campaign(campaign_id)
                    if item.get("work_item_id") == work_item_id]
        if len(packages) > 1:
            active = [item for item in packages if item.get("status") not in {
                "rejected", "superseded"
            }]
            if len(active) != 1:
                raise KnowledgeBuilderError("Workflow preparation identity is ambiguous.")
            package = active[0]
        else:
            package = packages[0] if packages else None
        proposal = None
        safety_review = None
        if package and package.get("workflow_plan"):
            try:
                proposal = self.workflows.proposal(package["generation_id"])
                safety_review = self.workflows.safety_exception_review(
                    package["generation_id"]
                )
            except KnowledgeWorkflowGenerationError as error:
                proposal = {"supported": True, "eligible": False,
                            "blockers": [str(error)]}

        filename = (package or {}).get("content_studio_filename")
        draft = self.drafts.get_draft(filename) if filename else None
        validation = WorkflowValidationService().validate(draft) if draft else None
        publication = None
        lifecycle = None
        if draft:
            publication = self.publications.status(draft.get("workflow_id"))
            try:
                lifecycle = self.lifecycle_factory(
                    self.orchestration.repository_root
                ).project(draft.get("workflow_id"))
            except Exception:
                lifecycle = None

        current_hash = self.publications.content_hash(draft) if draft else ""
        published_hash = ((publication or {}).get("versions") or [{}])[0].get(
            "content_hash"
        ) if publication and publication.get("versions") else ""
        published = bool(current_hash and current_hash == published_hash)

        step = "prepare"
        status = "working"
        attention = None
        if published:
            step, status = "complete", "published"
        elif draft and package and package.get("status") == "handed_off":
            step, status = "complete", "ready"
        elif package and package.get("workflow_plan") and state.get("next_action") in {
            "approve_workflow_draft_creation", "review_workflow_draft"
        }:
            step, status = "review", "ready" if proposal and proposal.get("eligible") else "needs_attention"
        elif state.get("action_authority") == "human_gate":
            status = "needs_attention"
            review_url = state.get("review_link")
            exception_count = None
            if state.get("next_action") == "review_evidence":
                extraction_id = str(state.get("package_id") or "")
                try:
                    exceptions = self._evidence_exception_workspace(
                        campaign_id, work_item_id, extraction_id
                    )["exceptions"]
                except KnowledgeBuilderError:
                    exceptions = []
                exception_count = len(exceptions)
                base = (f"/curator/growth/knowledge-builder/{campaign_id}/"
                        f"{work_item_id}/exceptions/evidence")
                review_url = (
                    f"{base}/{extraction_id}/{exceptions[0]['evidence_id']}"
                    if len(exceptions) == 1 else base
                )
            attention = {
                "title": self._human_action(state.get("next_action")),
                "reason": (state.get("blocker") or {}).get("explanation")
                or "A governed human decision is required before safe processing can continue.",
                "url": review_url,
                "count": exception_count,
            }
        elif state.get("blocker"):
            status = "needs_attention"
            attention = {
                "title": "Review preparation issue",
                "reason": state["blocker"].get("explanation"),
                "url": state.get("blocker_link") or state.get("review_link"),
            }

        reasoning_ready = bool(
            lifecycle is not None
            and not lifecycle.reasoning_review_error
            and all(item.review_status == "accepted"
                    for item in lifecycle.reasoning_reviews)
        )
        sources = sorted({url for node in ((package or {}).get("workflow_plan") or {}).get("nodes", [])
                          for url in node.get("source_urls") or []})
        return {
            "campaign_id": campaign_id,
            "work_item_id": work_item_id,
            "orchestration_id": record["orchestration_id"],
            "title": ((proposal or {}).get("workflow") or {}).get("name")
            or (package or {}).get("proposed_workflow_id")
            or state.get("title") or "Workflow knowledge",
            "step": step,
            "status": status,
            "steps": [
                {"id": "prepare", "label": "Prepare", "active": step == "prepare",
                 "complete": step in {"review", "complete"}},
                {"id": "review", "label": "Review", "active": step == "review",
                 "complete": step == "complete"},
                {"id": "complete", "label": "Complete", "active": step == "complete",
                 "complete": published},
            ],
            "progress_label": self._progress_label(state, package),
            "attention": attention,
            "state": deepcopy(state),
            "package": package,
            "proposal": proposal,
            "safety_review": safety_review,
            "draft": draft,
            "draft_filename": filename,
            "draft_fingerprint": current_hash,
            "validation": validation,
            "reasoning_ready": reasoning_ready,
            "reasoning_findings": list((lifecycle.validation.reasoning_findings
                                         if lifecycle else ())),
            "published": published,
            "publication": publication,
            "counts": {
                "sources": len(sources),
                "evidence": len((package or {}).get("approved_evidence_ids") or []),
                "claims": len((package or {}).get("approved_claim_ids") or []),
            },
            "sources": sources,
            "campaign_url": f"/curator/growth/coverage-campaigns/{campaign_id}/orchestration",
            "read_only": True,
        }

    def safety_exceptions(self, campaign_id: str, work_item_id: str,
                          node_id: str | None = None) -> dict[str, Any]:
        builder = self.project(campaign_id, work_item_id)
        package = builder.get("package") or {}
        if builder.get("step") != "review" or not package.get("generation_id"):
            raise KnowledgeBuilderError(
                "This workflow is not at the governed Builder review step."
            )
        try:
            review = self.workflows.safety_exception_review(
                package["generation_id"], node_id
            )
        except KnowledgeWorkflowGenerationError as error:
            raise KnowledgeBuilderError(str(error)) from error
        if (
            review.get("campaign_id") != campaign_id
            or review.get("work_item_id") != work_item_id
            or not review.get("exceptions")
        ):
            raise KnowledgeBuilderError(
                "The Builder safety exception is missing or no longer current."
            )
        return {
            **review,
            "builder_url": (f"/curator/growth/knowledge-builder/{campaign_id}/"
                            f"{work_item_id}"),
            "legacy_url": (builder.get("state") or {}).get("review_link")
            or builder["campaign_url"],
        }

    def decide_safety_exception(
        self, campaign_id: str, work_item_id: str, generation_id: str,
        node_id: str, decision: str, *, reviewer: str,
        proposal_fingerprint: str, exception_fingerprint: str,
        notes: str = "",
    ) -> dict[str, Any]:
        current = self.safety_exceptions(campaign_id, work_item_id, node_id)
        if current.get("generation_id") != generation_id:
            raise KnowledgeBuilderError("The workflow generation identity changed.")
        try:
            self.workflows.review_safety_exception(
                generation_id, node_id, decision, reviewer=reviewer,
                expected_proposal_fingerprint=proposal_fingerprint,
                expected_exception_fingerprint=exception_fingerprint,
                notes=notes,
            )
            builder = self.project(campaign_id, work_item_id)
        except KnowledgeWorkflowGenerationError as error:
            raise KnowledgeBuilderError(str(error)) from error
        return builder

    def prepare(self, campaign_id: str, work_item_id: str) -> dict[str, Any]:
        _, _, record, _ = self._resolve(campaign_id, work_item_id)
        if record.get("mode") != "supervised":
            raise KnowledgeBuilderError("Knowledge Builder requires supervised campaign mode.")
        external = 0
        transitions = 0
        for _ in range(self.MAX_SAFE_TRANSITIONS):
            current = self.orchestration.refresh(record["orchestration_id"])
            states = [item for item in current.get("work_item_states") or []
                      if item.get("work_item_id") == work_item_id]
            if len(states) != 1:
                raise KnowledgeBuilderError("Campaign work identity changed during preparation.")
            state = states[0]
            action = state.get("next_action")
            policy = ACTION_POLICY.get(action, {})
            if state.get("action_authority") != "machine_safe" or policy.get("authority") != "machine_safe":
                break
            if policy.get("external") and external >= self.MAX_EXTERNAL_OPERATIONS:
                break
            result = self.orchestration.advance_item(
                record["orchestration_id"], work_item_id, actor="Knowledge Builder"
            )
            transitions += 1
            external += int(bool(policy.get("external")))
            outcome = ((result.get("execution") or {}).get("outcomes") or [{}])[0]
            if outcome.get("status") not in {"completed", "package_reused"}:
                break
        projection = self.project(campaign_id, work_item_id)
        projection["execution"] = {"transitions": transitions,
                                   "external_operations": external}
        return projection

    def evidence_exceptions(self, campaign_id: str, work_item_id: str,
                            evidence_id: str | None = None) -> dict[str, Any]:
        _, _, _, state = self._resolve(campaign_id, work_item_id)
        extraction_id = str(state.get("package_id") or "")
        result = self._evidence_exception_workspace(
            campaign_id, work_item_id, extraction_id
        )
        if evidence_id is not None:
            matches = [unit for unit in result["exceptions"]
                       if unit.get("evidence_id") == evidence_id]
            if len(matches) != 1:
                raise KnowledgeBuilderError(
                    "The requested evidence exception is no longer unresolved."
                )
            result["current"] = matches[0]
        return result

    def decide_evidence_role(self, campaign_id: str, work_item_id: str,
                             extraction_id: str, evidence_id: str,
                             role: str) -> dict[str, Any]:
        current = self.evidence_exceptions(campaign_id, work_item_id, evidence_id)
        if current["extraction_id"] != extraction_id:
            raise KnowledgeBuilderError("The evidence package identity changed.")
        if role not in {"candidate", "context"}:
            raise KnowledgeBuilderError("Choose Candidate Evidence or Reviewer Context.")
        try:
            self.evidence.set_candidacy_role(extraction_id, evidence_id, role)
            workspace = self.evidence.review_workspace(extraction_id)
            if workspace.get("candidacy_ready_to_confirm"):
                self.evidence.confirm_candidate_set(extraction_id)
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error
        self._refresh_settled_evidence_gate(campaign_id, extraction_id)
        return self._evidence_exception_workspace(
            campaign_id, work_item_id, extraction_id, require_active_gate=False
        )

    def decide_evidence(self, campaign_id: str, work_item_id: str,
                        extraction_id: str, evidence_id: str,
                        decision: str, notes: str = "") -> dict[str, Any]:
        current = self.evidence_exceptions(campaign_id, work_item_id, evidence_id)
        if current["extraction_id"] != extraction_id:
            raise KnowledgeBuilderError("The evidence package identity changed.")
        try:
            self.evidence.review_evidence(
                extraction_id, evidence_id, decision, notes
            )
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error
        self._refresh_settled_evidence_gate(campaign_id, extraction_id)
        return self._evidence_exception_workspace(
            campaign_id, work_item_id, extraction_id, require_active_gate=False
        )

    def approve_workflow(self, campaign_id: str, work_item_id: str, *,
                         reviewer: str, proposal_fingerprint: str) -> dict[str, Any]:
        current = self.project(campaign_id, work_item_id)
        package = current.get("package") or {}
        proposal = current.get("proposal") or {}
        if current.get("step") != "review" or not proposal.get("eligible"):
            raise KnowledgeBuilderError("The workflow is not ready for governed approval.")
        if proposal_fingerprint != proposal.get("proposal_fingerprint"):
            raise KnowledgeBuilderError("The workflow proposal changed. Review it again.")
        try:
            self.workflows.approve_draft_creation(
                package["generation_id"], reviewer=reviewer,
                expected_proposal_fingerprint=proposal_fingerprint,
                notes="Approved in Unified Knowledge Builder.",
            )
            self.orchestration.refresh(current["orchestration_id"])
        except (KnowledgeWorkflowGenerationError,
                KnowledgeCampaignOrchestrationError) as error:
            raise KnowledgeBuilderError(str(error)) from error
        return self.project(campaign_id, work_item_id)

    def publish(self, campaign_id: str, work_item_id: str, *,
                expected_draft_fingerprint: str) -> dict[str, Any]:
        current = self.project(campaign_id, work_item_id)
        draft = current.get("draft")
        filename = current.get("draft_filename")
        if not draft or not filename:
            raise KnowledgeBuilderError("The approved workflow draft is unavailable.")
        if current.get("published"):
            return current
        if expected_draft_fingerprint != current.get("draft_fingerprint"):
            raise KnowledgeBuilderError("The workflow draft changed. Review it again before publishing.")
        if not (current.get("validation") or {}).get("is_valid"):
            raise KnowledgeBuilderError("The workflow draft must pass final validation.")
        if not current.get("reasoning_ready"):
            raise KnowledgeBuilderError(
                "Deterministic reasoning findings require explicit publication review."
            )
        try:
            self.publications.publish(draft, source_filename=filename)
        except WorkflowPublicationError as error:
            raise KnowledgeBuilderError(str(error)) from error
        return self.project(campaign_id, work_item_id)

    def _resolve(self, campaign_id: str, work_item_id: str):
        records = [item for item in self.orchestration.read_persisted(
            self.orchestration.campaign_root
        ) if item.get("campaign_id") == campaign_id]
        if len(records) != 1:
            raise KnowledgeBuilderError("Campaign orchestration is missing or ambiguous.")
        campaign = self.orchestration.planner.get(campaign_id)
        works = [item for item in campaign.get("work_items") or []
                 if item.get("work_item_id") == work_item_id
                 and item.get("work_type") == "workflow"]
        states = [item for item in records[0].get("work_item_states") or []
                  if item.get("work_item_id") == work_item_id]
        if len(works) != 1 or len(states) != 1:
            raise KnowledgeBuilderError("Workflow knowledge identity is missing or ambiguous.")
        return campaign, works[0], records[0], states[0]

    def _evidence_exception_workspace(self, campaign_id: str, work_item_id: str,
                                      extraction_id: str, *,
                                      require_active_gate: bool = True) -> dict[str, Any]:
        if not extraction_id:
            raise KnowledgeBuilderError("The evidence exception package is unavailable.")
        _, _, _, state = self._resolve(campaign_id, work_item_id)
        if require_active_gate and (
            state.get("action_authority") != "human_gate"
            or state.get("next_action") != "review_evidence"
            or str(state.get("package_id") or "") != extraction_id
        ):
            raise KnowledgeBuilderError(
                "This evidence package is not the current Builder-blocking exception."
            )
        try:
            workspace = self.evidence.review_workspace(extraction_id)
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error
        package = workspace.get("package") or {}
        compression = workspace.get("compression") or {}
        if (
            package.get("campaign_id") != campaign_id
            or package.get("work_item_id") != work_item_id
            or package.get("extraction_id") != extraction_id
            or not compression.get("enabled")
        ):
            raise KnowledgeBuilderError(
                "The compressed evidence exception identity is missing or ambiguous."
            )
        exceptions = list(compression.get("exception_units") or [])
        context = workspace.get("context") or {}
        canonical_source_url = str(package.get("canonical_source_url") or "")
        source_domain = urlsplit(canonical_source_url).hostname or ""
        return {
            "campaign_id": campaign_id,
            "work_item_id": work_item_id,
            "extraction_id": extraction_id,
            "title": context.get("campaign_title") or package.get("workflow_name")
            or "Workflow knowledge",
            "capability": context.get("facet") or context.get("area") or "",
            "source": {
                "title": package.get("source_title") or "Approved source",
                "publisher": package.get("publisher") or "",
                "domain": source_domain,
                "canonical_url": canonical_source_url,
            },
            "exceptions": exceptions,
            "current": None,
            "legacy_url": f"/curator/growth/evidence-extraction/{extraction_id}",
            "builder_url": (f"/curator/growth/knowledge-builder/{campaign_id}/"
                            f"{work_item_id}"),
            "candidate_set_current": workspace.get("candidate_set_current"),
            "read_only": True,
        }

    def _refresh_settled_evidence_gate(self, campaign_id: str,
                                       extraction_id: str) -> None:
        workspace = self.evidence.review_workspace(extraction_id)
        if (workspace.get("compression") or {}).get("exception_units"):
            return
        records = [item for item in self.orchestration.read_persisted(
            self.orchestration.campaign_root
        ) if item.get("campaign_id") == campaign_id]
        if len(records) != 1:
            raise KnowledgeBuilderError("Campaign orchestration is missing or ambiguous.")
        try:
            self.orchestration.refresh(records[0]["orchestration_id"])
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error

    @staticmethod
    def _human_action(action: str | None) -> str:
        return {
            "approve_source": "Review source package",
            "review_evidence": "Review evidence exception",
            "review_claims": "Review claim exception",
            "approve_workflow_draft_creation": "Review workflow",
            "review_workflow_draft": "Review workflow",
        }.get(action, "Review exception")

    @staticmethod
    def _progress_label(state: dict[str, Any], package: dict[str, Any] | None) -> str:
        action = state.get("next_action")
        if action in {"prepare_research", "run_source_research", "approve_source"}:
            return "Researching"
        if action in {"prepare_evidence", "extract_evidence", "review_evidence"}:
            return "Verifying evidence"
        if action in {"prepare_workflow_claim_plan", "plan_workflow_claims", "review_claims",
                      "prepare_workflow_package", "plan_workflow"}:
            return "Building knowledge"
        if package and package.get("status") in {"plan_ready", "draft_ready",
                                                  "approved_for_handoff", "handed_off"}:
            return "Draft ready"
        return "Preparing knowledge"
