from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from app.services.knowledge_campaign_orchestration_service import (
    ACTION_POLICY,
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_claim_planning_service import (
    KnowledgeClaimPlanningError,
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
                 publications=None, lifecycle_factory=None, evidence=None,
                 claims=None):
        self.orchestration = orchestration or KnowledgeCampaignOrchestrationService()
        self.workflows = workflows or self.orchestration.workflows
        self.evidence = evidence or self.orchestration.evidence
        self.claims = claims or self.orchestration.claims
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
            exception_proposition_count = None
            if state.get("next_action") == "review_evidence":
                extraction_id = str(state.get("package_id") or "")
                try:
                    exception_workspace = self._evidence_exception_workspace(
                        campaign_id, work_item_id, extraction_id
                    )
                    exceptions = exception_workspace["exceptions"]
                    exception_count = len(exception_workspace["groups"])
                    exception_proposition_count = len(exceptions)
                except KnowledgeBuilderError:
                    exceptions = []
                    exception_count = 0
                    exception_proposition_count = 0
                base = (f"/curator/growth/knowledge-builder/{campaign_id}/"
                        f"{work_item_id}/exceptions/evidence")
                review_url = (
                    f"{base}/{extraction_id}/{exceptions[0]['evidence_id']}"
                    if len(exceptions) == 1 else base
                )
            elif state.get("next_action") == "review_claims":
                plan_id = str(state.get("package_id") or "")
                self._claim_review_workspace(
                    campaign_id, work_item_id, plan_id
                )
                review_url = self._claim_review_url(
                    campaign_id, work_item_id, plan_id
                )
            attention = {
                "title": self._human_action(state.get("next_action")),
                "reason": (state.get("blocker") or {}).get("explanation")
                or "A governed human decision is required before safe processing can continue.",
                "url": review_url,
                "count": exception_count,
                "proposition_count": exception_proposition_count,
                "kind": (
                    "claim_review"
                    if state.get("next_action") == "review_claims"
                    else "evidence_review"
                    if state.get("next_action") == "review_evidence"
                    else None
                ),
                "plan_id": (
                    str(state.get("package_id") or "")
                    if state.get("next_action") == "review_claims"
                    else None
                ),
            }
        elif state.get("blocker"):
            status = "needs_attention"
            plan_id = str(state.get("package_id") or "")
            blocker = state.get("blocker") or {}
            if blocker.get("blocker_type") == "verification_evidence_exhausted":
                attention = {
                    "title": "Verification evidence could not be established",
                    "reason": blocker.get("explanation"),
                    "url": None,
                    "kind": "verification_recovery_exhausted",
                    "recovery": deepcopy(state.get("verification_recovery") or {}),
                }
                claim_review = None
            else:
                try:
                    claim_review = self._claim_review_workspace(
                        campaign_id, work_item_id, plan_id
                    )
                except KnowledgeBuilderError:
                    claim_review = None
            if attention is None:
                if claim_review is not None:
                    attention = {
                        "title": "Review claims",
                        "reason": state["blocker"].get("explanation"),
                        "url": self._claim_review_url(
                            campaign_id, work_item_id, plan_id
                        ),
                        "count": claim_review["compression"].get(
                            "unresolved_attention_count", 0
                        ),
                        "kind": "claim_review",
                        "plan_id": plan_id,
                    }
                else:
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
            "prepare_action": self._prepare_action(state),
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

    def claim_review(self, campaign_id: str, work_item_id: str,
                     plan_id: str) -> dict[str, Any]:
        """Project one exact Builder-owned claim review without writes."""
        return self._claim_review_workspace(campaign_id, work_item_id, plan_id)

    def decide_claim_exception(
        self, campaign_id: str, work_item_id: str, plan_id: str,
        claim_id: str, decision: str, *, expected_plan_fingerprint: str,
        notes: str = "",
    ) -> dict[str, Any]:
        workspace = self._claim_review_workspace(
            campaign_id, work_item_id, plan_id
        )
        compression = workspace["compression"]
        if expected_plan_fingerprint != compression.get("plan_fingerprint"):
            raise KnowledgeBuilderError(
                "The claim plan changed. Review it again."
            )
        matches = [
            claim for claim in compression.get("exception_claims") or []
            if claim.get("claim_id") == claim_id
        ]
        if len(matches) != 1:
            raise KnowledgeBuilderError(
                "The requested claim exception is no longer current."
            )
        try:
            self.claims.review_claim(plan_id, claim_id, decision, notes)
        except KnowledgeClaimPlanningError as error:
            raise KnowledgeBuilderError(str(error)) from error
        return self._claim_review_workspace(campaign_id, work_item_id, plan_id)

    def approve_claim_set(
        self, campaign_id: str, work_item_id: str, plan_id: str, *,
        reviewer: str, expected_plan_fingerprint: str,
        expected_evidence_fingerprint: str,
    ) -> dict[str, Any]:
        """Approve one reviewed set, then continue only this Builder item."""
        workspace = self._claim_review_workspace(
            campaign_id, work_item_id, plan_id,
            require_active_boundary=False,
        )
        plan = workspace["plan"]
        prior = plan.get("claim_set_approval") or {}
        duplicate = (
            prior.get("decision") == "approved"
            and prior.get("plan_fingerprint") == expected_plan_fingerprint
            and prior.get("evidence_input_fingerprint")
            == expected_evidence_fingerprint
        )
        if not duplicate and not self._claim_boundary_matches(
            workspace["state"], plan_id, allow_blocked=False
        ):
            raise KnowledgeBuilderError(
                "The claim plan is no longer at its governed Builder review boundary."
            )
        try:
            approved = self.claims.approve_reviewed_claim_set(
                plan_id,
                expected_plan_fingerprint=expected_plan_fingerprint,
                expected_evidence_fingerprint=expected_evidence_fingerprint,
                reviewer=reviewer,
            )
        except KnowledgeClaimPlanningError as error:
            raise KnowledgeBuilderError(str(error)) from error
        if approved.get("claim_set_approval_result") == "already_approved":
            result = self.project(campaign_id, work_item_id)
            result["claim_set_approval_result"] = "already_approved"
            return result
        try:
            result = self.prepare(campaign_id, work_item_id)
        except KnowledgeBuilderError as error:
            # Claim-set approval is already an authoritative atomic commit. A
            # later bounded-continuation failure must not misreport or replay it.
            result = self.project(campaign_id, work_item_id)
            result["continuation_error"] = str(error)
        result["claim_set_approval_result"] = "approved"
        return result

    def recover_verification_evidence(
        self, campaign_id: str, work_item_id: str, plan_id: str, *,
        expected_gap_fingerprint: str,
        expected_evidence_fingerprint: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """Return one exact missing-verification plan to governed research."""
        workspace = self._claim_review_workspace(
            campaign_id, work_item_id, plan_id
        )
        recovery = workspace["gap_recovery"]
        if not recovery.get("eligible"):
            raise KnowledgeBuilderError(
                recovery.get("reason")
                or "This claim plan is not eligible for targeted evidence recovery."
            )
        if (
            expected_gap_fingerprint != recovery.get("gap_fingerprint")
            or expected_evidence_fingerprint
            != recovery.get("evidence_input_fingerprint")
        ):
            raise KnowledgeBuilderError(
                "The evidence gap changed. Review the current claim plan again."
            )
        campaign, work, _, _ = self._resolve(campaign_id, work_item_id)
        gaps = [item for item in campaign.get("gaps") or []
                if item.get("gap_id") == work.get("gap_id")]
        if len(gaps) != 1:
            raise KnowledgeBuilderError(
                "The campaign evidence-gap identity is missing or ambiguous."
            )
        try:
            research = self.orchestration.research.create_verification_recovery(
                campaign_id, gaps[0]["gap_id"], work_item_id,
                claim_plan_id=plan_id,
                evidence_input_fingerprint=expected_evidence_fingerprint,
                gap_fingerprint=expected_gap_fingerprint,
                actor=reviewer,
            )
            result = self.prepare(campaign_id, work_item_id)
        except Exception as error:
            if isinstance(error, KnowledgeBuilderError):
                raise
            raise KnowledgeBuilderError(str(error)) from error
        result["verification_recovery"] = {
            "research_package_id": research["package_id"],
            "objective": deepcopy(research.get("research_objective") or {}),
        }
        return result

    def prepare(self, campaign_id: str, work_item_id: str) -> dict[str, Any]:
        _, _, record, _ = self._resolve(campaign_id, work_item_id)
        if record.get("mode") != "supervised":
            raise KnowledgeBuilderError("Knowledge Builder requires supervised campaign mode.")
        external = 0
        transitions = 0
        stop_reason = None
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
                stop_reason = "governed_boundary"
                break
            if policy.get("external") and external >= self.MAX_EXTERNAL_OPERATIONS:
                stop_reason = "external_operation_boundary"
                break
            try:
                result = self.orchestration.advance_item(
                    record["orchestration_id"], work_item_id,
                    actor="Knowledge Builder",
                )
            except KnowledgeCampaignOrchestrationError as error:
                raise KnowledgeBuilderError(str(error)) from error
            transitions += 1
            external += int(bool(policy.get("external")))
            outcome = ((result.get("execution") or {}).get("outcomes") or [{}])[0]
            if outcome.get("status") not in {"completed", "package_reused"}:
                stop_reason = "preparation_failed"
                break
        else:
            current = self.orchestration.refresh(record["orchestration_id"])
            states = [item for item in current.get("work_item_states") or []
                      if item.get("work_item_id") == work_item_id]
            if len(states) == 1 and states[0].get("action_authority") == "machine_safe":
                stop_reason = "safety_ceiling"
        projection = self.project(campaign_id, work_item_id)
        projection["execution"] = {"transitions": transitions,
                                   "external_operations": external,
                                   "stop_reason": stop_reason}
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

    def evidence_exception_group(self, campaign_id: str, work_item_id: str,
                                 group_id: str) -> dict[str, Any]:
        result = self.evidence_exceptions(campaign_id, work_item_id)
        matches = [group for group in result["groups"]
                   if group.get("group_id") == group_id]
        if len(matches) != 1:
            raise KnowledgeBuilderError(
                "The requested evidence review group is no longer current."
            )
        result["current_group"] = matches[0]
        return result

    def decide_evidence_group(
        self, campaign_id: str, work_item_id: str, extraction_id: str,
        group_id: str, decision: str, *, expected_group_fingerprint: str,
        reviewer: str, notes: str = "",
    ) -> dict[str, Any]:
        decision = str(decision or "").strip()
        recorded_decision = (
            f"role:{decision}" if decision in {"candidate", "context"}
            else f"evidence:{decision}"
        )
        try:
            package = self.evidence.get(extraction_id)
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error
        if self.evidence._group_decision_recorded(
            package, group_id, expected_group_fingerprint, recorded_decision
        ):
            return self._evidence_exception_workspace(
                campaign_id, work_item_id, extraction_id,
                require_active_gate=False,
            )
        current = self.evidence_exception_group(
            campaign_id, work_item_id, group_id
        )
        group = current["current_group"]
        if current["extraction_id"] != extraction_id:
            raise KnowledgeBuilderError("The evidence package identity changed.")
        if group["group_fingerprint"] != expected_group_fingerprint:
            raise KnowledgeBuilderError(
                "The evidence review group changed. Review it again."
            )
        members = group["member_preconditions"]
        try:
            if group["decision_mode"] == "role" and decision in {
                "candidate", "context"
            }:
                self.evidence.set_candidacy_role_group(
                    extraction_id, members, decision,
                    group_id=group_id,
                    group_fingerprint=expected_group_fingerprint,
                    actor=reviewer,
                )
            elif group["decision_mode"] == "evidence" and decision in {
                "approved", "rejected", "needs_revision"
            }:
                self.evidence.review_evidence_group(
                    extraction_id, members, decision, notes,
                    group_id=group_id,
                    group_fingerprint=expected_group_fingerprint,
                    actor=reviewer,
                )
            else:
                raise KnowledgeBuilderError(
                    "That decision is not valid for the current evidence group."
                )
        except KnowledgeBuilderError:
            raise
        except Exception as error:
            raise KnowledgeBuilderError(str(error)) from error
        self._refresh_settled_evidence_gate(campaign_id, extraction_id)
        return self._evidence_exception_workspace(
            campaign_id, work_item_id, extraction_id,
            require_active_gate=False,
        )

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
        try:
            record = self.orchestration.project_current(
                records[0]["orchestration_id"]
            )
        except KnowledgeCampaignOrchestrationError as error:
            raise KnowledgeBuilderError(str(error)) from error
        if record.get("campaign_id") != campaign_id:
            raise KnowledgeBuilderError("Campaign orchestration identity changed.")
        campaign = self.orchestration.planner.get(campaign_id)
        works = [item for item in campaign.get("work_items") or []
                 if item.get("work_item_id") == work_item_id
                 and item.get("work_type") == "workflow"]
        states = [item for item in record.get("work_item_states") or []
                  if item.get("work_item_id") == work_item_id]
        if len(works) != 1 or len(states) != 1:
            raise KnowledgeBuilderError("Workflow knowledge identity is missing or ambiguous.")
        return campaign, works[0], record, states[0]

    def _claim_review_workspace(
        self, campaign_id: str, work_item_id: str, plan_id: str, *,
        require_active_boundary: bool = True,
    ) -> dict[str, Any]:
        if not plan_id:
            raise KnowledgeBuilderError("The workflow claim plan is unavailable.")
        _, _, record, state = self._resolve(campaign_id, work_item_id)
        try:
            workspace = self.claims.review_workspace(plan_id)
        except KnowledgeClaimPlanningError as error:
            raise KnowledgeBuilderError(str(error)) from error
        plan = workspace.get("plan") or {}
        compression = workspace.get("compression") or {}
        if (
            plan.get("claim_plan_id") != plan_id
            or plan.get("campaign_id") != campaign_id
            or plan.get("work_item_id") != work_item_id
            or plan.get("target_asset_type") != "workflow"
            or not compression.get("enabled")
        ):
            raise KnowledgeBuilderError(
                "The compressed workflow claim-review identity is missing or ambiguous."
            )
        if require_active_boundary and not self._claim_boundary_matches(
            state, plan_id, allow_blocked=True
        ):
            raise KnowledgeBuilderError(
                "This claim plan is not the current Builder review boundary."
            )
        return {
            "campaign_id": campaign_id,
            "work_item_id": work_item_id,
            "orchestration_id": record["orchestration_id"],
            "plan_id": plan_id,
            "title": state.get("title") or "Workflow knowledge",
            "plan": plan,
            "compression": compression,
            "gap_recovery": self._verification_gap_recovery(
                plan, compression
            ),
            "state": deepcopy(state),
            "builder_url": (
                f"/curator/growth/knowledge-builder/{campaign_id}/{work_item_id}"
            ),
            "legacy_url": f"/curator/growth/claim-planning/{plan_id}",
            "read_only": True,
        }

    def _verification_gap_recovery(
        self, plan: dict[str, Any], compression: dict[str, Any]
    ) -> dict[str, Any]:
        """Project the one allowlisted evidence-gap recovery without writes."""
        gaps = list(plan.get("evidence_gaps") or [])
        stale_ids = list((plan.get("validation") or {}).get(
            "stale_evidence_ids"
        ) or [])
        reason = ""
        try:
            input_current = bool(self.claims.input_is_current(
                plan.get("claim_plan_id")
            ))
        except Exception:
            input_current = False
        verification_only = bool(gaps) and all(
            gap.get("required") is not False
            and str(gap.get("section") or "").casefold()
            in {"verification", "expected_result"}
            and str(gap.get("coverage_role") or "success_verification")
            == "success_verification"
            for gap in gaps
        )
        if plan.get("status") != "needs_evidence":
            reason = "The current claim plan is not blocked on missing evidence."
        elif compression.get("unresolved_attention_count"):
            reason = "Claim exceptions must be resolved before targeted evidence recovery."
        elif compression.get("conflict_count"):
            reason = "Claim conflicts prevent targeted evidence recovery."
        elif stale_ids or not input_current:
            reason = "The approved-evidence fingerprint is stale or could not be verified."
        elif not verification_only:
            reason = "The plan has evidence gaps outside the bounded verification objective."
        canonical_gaps = [{key: gap.get(key) for key in (
            "gap_id", "section", "required", "coverage_role", "reason"
        )} for gap in sorted(gaps, key=lambda item: str(item.get("gap_id") or ""))]
        gap_fingerprint = hashlib.sha256(json.dumps({
            "claim_plan_id": plan.get("claim_plan_id"),
            "campaign_id": plan.get("campaign_id"),
            "work_item_id": plan.get("work_item_id"),
            "evidence_input_fingerprint": compression.get(
                "evidence_input_fingerprint"
            ),
            "plan_fingerprint": compression.get("plan_fingerprint"),
            "gaps": canonical_gaps,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )).hexdigest()
        return {
            "eligible": not reason,
            "kind": "missing_verification_evidence",
            "title": "Missing verification evidence",
            "summary": (
                "Gnojo has enough evidence to describe the procedure, but not "
                "enough approved evidence to determine how the user confirms "
                "the procedure succeeded."
            ),
            "technical_gap_count": len(gaps),
            "gap_fingerprint": gap_fingerprint,
            "evidence_input_fingerprint": compression.get(
                "evidence_input_fingerprint"
            ),
            "reason": reason,
            "objective": {
                "supported_evidence_types": ["verification", "expected_result"],
                "required_coverage_roles": ["success_verification"],
            },
        }

    @staticmethod
    def _claim_boundary_matches(
        state: dict[str, Any], plan_id: str, *, allow_blocked: bool,
    ) -> bool:
        if str(state.get("package_id") or "") != plan_id:
            return False
        if (
            state.get("action_authority") == "human_gate"
            and state.get("next_action") == "review_claims"
        ):
            return True
        blocker = state.get("blocker") or {}
        return bool(
            allow_blocked
            and state.get("state") == "blocked"
            and str(blocker.get("original_package_id") or "") == plan_id
            and blocker.get("blocker_type") in {
                "workflow_eligibility", "claim_state"
            }
        )

    @staticmethod
    def _claim_review_url(
        campaign_id: str, work_item_id: str, plan_id: str,
    ) -> str:
        return (
            f"/curator/growth/knowledge-builder/{campaign_id}/{work_item_id}"
            f"/claims/{plan_id}"
        )

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
        groups = self._group_evidence_exceptions(
            extraction_id, exceptions,
            candidate_set_current=bool(workspace.get("candidate_set_current")),
        )
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
            "groups": groups,
            "review_count": len(groups),
            "current": None,
            "current_group": None,
            "legacy_url": f"/curator/growth/evidence-extraction/{extraction_id}",
            "builder_url": (f"/curator/growth/knowledge-builder/{campaign_id}/"
                            f"{work_item_id}"),
            "candidate_set_current": workspace.get("candidate_set_current"),
            "read_only": True,
        }

    @classmethod
    def _group_evidence_exceptions(
        cls, extraction_id: str, exceptions: list[dict[str, Any]], *,
        candidate_set_current: bool,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for unit in exceptions:
            candidacy = unit.get("candidacy") or {}
            compression = unit.get("workflow_evidence_compression") or {}
            reason = str(compression.get("reason") or "human_exception")
            role = candidacy.get("human_confirmed_role") or "unresolved"
            mode = (
                "role" if role == "unresolved"
                else "evidence" if role == "candidate" and candidate_set_current
                else "waiting"
            )
            recommendation = str(
                candidacy.get("machine_recommended_role") or "undetermined"
            )
            coverage_roles = tuple(sorted(unit.get("workflow_coverage_roles") or []))
            if reason == "potentially_conflicting_evidence":
                compatibility = (unit.get("evidence_id"),)
            elif reason == "unsafe_or_state_changing_content":
                compatibility = ("shared_state_change_review",)
            else:
                compatibility = (
                    str(unit.get("evidence_type") or "unspecified"), coverage_roles,
                )
            key = (reason, recommendation, role, mode, compatibility)
            grouped.setdefault(key, []).append(unit)

        results = []
        for key, members in grouped.items():
            reason, recommendation, role, mode, _ = key
            members = sorted(members, key=lambda item: item.get("evidence_id") or "")
            identities = [
                {
                    "evidence_id": unit.get("evidence_id"),
                    "evidence_fingerprint": unit.get("fingerprint"),
                    "recommendation_fingerprint": (
                        unit.get("candidacy") or {}
                    ).get("recommendation_fingerprint"),
                    "human_confirmed_role": (
                        unit.get("candidacy") or {}
                    ).get("human_confirmed_role"),
                    "review_state": unit.get("review_state"),
                }
                for unit in members
            ]
            stable = {
                "extraction_id": extraction_id,
                "reason": reason,
                "recommendation": recommendation,
                "member_ids": [item["evidence_id"] for item in identities],
            }
            group_id = "KBEG-" + hashlib.sha256(
                json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:12].upper()
            current = {**stable, "mode": mode, "role": role,
                       "members": identities,
                       "candidate_set_current": candidate_set_current}
            group_fingerprint = hashlib.sha256(
                json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            title, explanation = cls._evidence_group_copy(reason, len(members))
            results.append({
                "group_id": group_id,
                "group_fingerprint": group_fingerprint,
                "title": title,
                "explanation": explanation,
                "reason": reason,
                "machine_recommendation": recommendation,
                "decision_mode": mode,
                "count": len(members),
                "grouped": len(members) > 1,
                "units": members,
                "member_preconditions": identities,
            })
        return sorted(results, key=lambda item: (
            item["title"], item["group_id"]
        ))

    @staticmethod
    def _evidence_group_copy(reason: str, count: int) -> tuple[str, str]:
        if reason == "unsafe_or_state_changing_content":
            return (
                "State-changing procedures",
                "These excerpts share the same source, safety classification, "
                "machine recommendation, and governed role decision.",
            )
        if reason == "ambiguous_evidence_role":
            return (
                "Supporting diagnostic context",
                "These excerpts share the same source and unresolved diagnostic "
                "evidence-role question.",
            )
        if reason == "potentially_conflicting_evidence":
            return (
                "Potential evidence conflict",
                "This proposition remains separate because potentially conflicting "
                "evidence cannot share a decision without a proven conflict set.",
            )
        return (
            "Evidence review exception" if count == 1 else "Compatible evidence exceptions",
            "These excerpts have the same source, review reason, role, and available decision.",
        )

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
            "review_claims": "Review claims",
            "approve_workflow_draft_creation": "Review workflow",
            "review_workflow_draft": "Review workflow",
        }.get(action, "Review exception")

    @staticmethod
    def _progress_label(state: dict[str, Any], package: dict[str, Any] | None) -> str:
        action = state.get("next_action")
        if action in {"prepare_research", "run_source_research", "approve_source"}:
            return "Researching"
        if (action in {"prepare_evidence", "extract_evidence", "review_evidence"}
                or state.get("stage") == "verification_evidence_exhausted"):
            return "Verifying evidence"
        if action in {"prepare_workflow_claim_plan", "plan_workflow_claims", "review_claims",
                      "prepare_workflow_package", "plan_workflow"}:
            return "Building knowledge"
        if package and package.get("status") in {"plan_ready", "draft_ready",
                                                  "approved_for_handoff", "handed_off"}:
            return "Draft ready"
        return "Preparing knowledge"

    @staticmethod
    def _prepare_action(state: dict[str, Any]) -> dict[str, str]:
        action = state.get("next_action")
        policy = ACTION_POLICY.get(action, {})
        if (
            state.get("verification_recovery")
            and action in {"prepare_evidence", "extract_evidence"}
        ):
            return {
                "label": "Retrieve Next Evidence Source",
                "explanation": (
                    "The next step processes one approved verification source. "
                    "Each external retrieval requires an explicit request."
                ),
            }
        if state.get("action_authority") == "machine_safe" and policy.get("external"):
            if action == "extract_evidence":
                return {
                    "label": "Retrieve Next Evidence Source",
                    "explanation": (
                        "The next step retrieves one approved external source. "
                        "Each external retrieval requires an explicit request."
                    ),
                }
            return {
                "label": "Continue External Preparation",
                "explanation": (
                    "The next step uses an approved external source operation and "
                    "requires an explicit request."
                ),
            }
        return {"label": "Prepare Knowledge", "explanation": ""}
