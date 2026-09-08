from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from app.data_root import resolve_data_root
from typing import Any

from app.services.knowledge_claim_planning_service import KnowledgeClaimPlanningService
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_draft_assembly_service import KnowledgeDraftAssemblyService
from app.services.knowledge_draft_generation_service import KnowledgeDraftGenerationService
from app.services.knowledge_evidence_extraction_service import KnowledgeEvidenceExtractionService
from app.services.knowledge_source_research_service import KnowledgeSourceResearchService
from app.services.knowledge_workflow_generation_service import KnowledgeWorkflowGenerationService
from app.services.campaign_review_destination_service import CampaignReviewDestinationService


class KnowledgeCampaignOrchestrationError(ValueError):
    pass


WORKFLOW_TYPES = {"workflow", "workflow_branch", "verification_step", "escalation_path", "safety_review"}

# Authority is declared centrally and is intentionally conservative. Route handlers do
# not decide whether a phase may be crossed.
ACTION_POLICY = {
    "analyze_coverage": {"authority": "machine_safe", "external": False},
    "prepare_research": {"authority": "machine_safe", "external": False},
    "run_source_research": {"authority": "machine_safe", "external": True},
    "prepare_evidence": {"authority": "machine_safe", "external": False},
    "extract_evidence": {"authority": "machine_safe", "external": True},
    "prepare_article_package": {"authority": "machine_safe", "external": False},
    "prepare_claim_plan": {"authority": "machine_safe", "external": False},
    "plan_claims": {"authority": "machine_safe", "external": False},
    "prepare_workflow_claim_plan": {"authority": "machine_safe", "external": False},
    "plan_workflow_claims": {"authority": "machine_safe", "external": False},
    "assemble_article": {"authority": "machine_safe", "external": False},
    "prepare_workflow_package": {"authority": "machine_safe", "external": False},
    "plan_workflow": {"authority": "machine_safe", "external": False},
    "prepare_workflow_draft": {"authority": "machine_safe", "external": False},
    "approve_source": {"authority": "human_gate", "external": False},
    "review_evidence": {"authority": "human_gate", "external": False},
    "review_claims": {"authority": "human_gate", "external": False},
    "review_article_draft": {"authority": "human_gate", "external": False},
    "review_workflow_draft": {"authority": "human_gate", "external": False},
    "accept_article_content_studio": {"authority": "human_gate", "external": False},
    "accept_workflow_content_studio": {"authority": "human_gate", "external": False},
    "publish": {"authority": "human_gate", "external": False},
    "author_learning_content": {"authority": "human_gate", "external": False},
    "review_command_reference": {"authority": "human_gate", "external": False},
}


class KnowledgeCampaignOrchestrationService:
    """Supervised, bounded coordination over the authoritative Phase 1-8 services."""

    def __init__(self, repository_root: Path | None = None, campaign_root: Path | None = None,
                 *, planner=None, research=None, evidence=None, generation=None,
                 claims=None, assembly=None, workflows=None, review_destinations=None, max_transitions: int = 24,
                 max_work_items: int = 12, max_external_operations: int = 1):
        self.repository_root = resolve_data_root(repository_root, legacy_root=Path(__file__).resolve().parents[2])
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.package_root = self.campaign_root / "orchestration"
        self.planner = planner or KnowledgeCoveragePlannerService(self.repository_root, self.campaign_root)
        self.research = research or KnowledgeSourceResearchService(self.repository_root, self.campaign_root)
        self.evidence = evidence or KnowledgeEvidenceExtractionService(self.repository_root, self.campaign_root)
        self.generation = generation or KnowledgeDraftGenerationService(self.repository_root, self.campaign_root)
        self.claims = claims or KnowledgeClaimPlanningService(self.generation, self.campaign_root)
        self.assembly = assembly or KnowledgeDraftAssemblyService(self.generation, self.campaign_root)
        self.workflows = workflows or KnowledgeWorkflowGenerationService(self.repository_root, self.campaign_root)
        self.review_destinations = review_destinations or CampaignReviewDestinationService(self.repository_root)
        self.limits = {
            "max_transitions": max(1, int(max_transitions)),
            "max_work_items": max(1, int(max_work_items)),
            "max_external_operations": max(0, int(max_external_operations)),
            "max_retries": 0,
        }

    @staticmethod
    def action_policy() -> dict[str, dict[str, Any]]:
        return deepcopy(ACTION_POLICY)

    @classmethod
    def for_command_relationship_decision(
        cls, repository_root: Path, campaign_root: Path | None = None
    ) -> "KnowledgeCampaignOrchestrationService":
        """Construct only the authorities needed by the bounded decision operation."""
        value = cls.__new__(cls)
        value.repository_root = Path(repository_root).resolve()
        value.campaign_root = Path(
            campaign_root or value.repository_root / "knowledge_campaigns"
        ).resolve()
        value.package_root = value.campaign_root / "orchestration"
        value.planner = KnowledgeCoveragePlannerService(
            value.repository_root, value.campaign_root
        )
        return value

    def get_or_create(self, campaign_id: str, mode: str = "supervised",
                      actor: str = "Human") -> dict[str, Any]:
        if mode not in {"manual", "supervised"}:
            raise KnowledgeCampaignOrchestrationError("Orchestration mode must be manual or supervised.")
        campaign = self.planner.get(campaign_id)
        orchestration_id = self._stable_id("KORCH", campaign_id)
        path = self._path(orchestration_id)
        if path.exists():
            return self.refresh(orchestration_id)
        now = self._now()
        record = {
            "schema_version": "1.0", "orchestration_id": orchestration_id,
            "campaign_id": campaign_id, "campaign_objective": campaign.get("objective", ""),
            "status": "active", "mode": mode, "work_item_states": [],
            "actionable_queue": [], "human_review_queue": [], "blockers": [],
            "stale_dependencies": [], "completed_items": [], "next_recommended_action": None,
            "pipeline_summary": {}, "readiness_summary": {}, "dependency_graph": {},
            "last_execution_at": None, "fingerprints": {}, "revisions": [],
            "history": [{"event": "orchestration_enabled", "at": now,
                         "actor": actor, "mode": mode}],
            "created_at": now, "updated_at": now,
        }
        self._save(record)
        return self.refresh(orchestration_id)

    def get(self, orchestration_id: str) -> dict[str, Any]:
        path = self._path(orchestration_id)
        if not path.exists():
            raise KnowledgeCampaignOrchestrationError(f"Orchestration '{orchestration_id}' was not found.")
        return self._read(path)

    @classmethod
    def read_persisted(cls, campaign_root: Path) -> list[dict[str, Any]]:
        """Read persisted orchestration records without constructing pipeline writers."""
        package_root = Path(campaign_root).resolve() / "orchestration"
        if not package_root.exists():
            return []
        return [cls._read(path) for path in sorted(package_root.glob("KORCH-*.json"))]

    def set_mode(self, orchestration_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"manual", "supervised"}:
            raise KnowledgeCampaignOrchestrationError("Orchestration mode must be manual or supervised.")
        record = self.get(orchestration_id)
        if record.get("mode") != mode:
            record["mode"] = mode
            self._event(record, "mode_changed", "Human", mode=mode)
            self._save(record)
        return self.refresh(orchestration_id)

    def refresh(self, orchestration_id: str) -> dict[str, Any]:
        record = self.get(orchestration_id)
        campaign = self.planner.get(record["campaign_id"])
        states = [self._resolve_item(campaign, item) for item in campaign.get("work_items") or []]
        projection = self._projection(campaign, states)
        fingerprint = self._fingerprint(projection)
        previous = record.get("fingerprints", {}).get("projection")
        if previous != fingerprint:
            if previous:
                record.setdefault("revisions", []).append({
                    "at": self._now(), "fingerprint": previous,
                    "status": record.get("status"), "readiness_summary": record.get("readiness_summary", {}),
                })
            record.update(projection)
            record.setdefault("fingerprints", {})["projection"] = fingerprint
            record["updated_at"] = self._now()
            self._save(record)
        return deepcopy(record)

    def continue_campaign(self, orchestration_id: str) -> dict[str, Any]:
        record = self.refresh(orchestration_id)
        if record.get("mode") != "supervised":
            raise KnowledgeCampaignOrchestrationError("Continue Campaign is available only in supervised mode.")
        outcomes, transitions, external = [], 0, 0
        candidate = record.get("next_recommended_action")
        if candidate and candidate.get("action_authority") == "machine_safe":
            policy = ACTION_POLICY.get(candidate.get("next_action"), {})
            if policy.get("authority") == "machine_safe":
                if policy.get("external") and self.limits["max_external_operations"] < 1:
                    outcomes.append({"work_item_id": candidate["work_item_id"], "status": "limit_reached",
                                     "action": candidate["next_action"],
                                     "message": "External operation limit reached."})
                else:
                    outcomes.append(self._execute(
                        record["campaign_id"], candidate["work_item_id"], candidate["next_action"]
                    ))
                    transitions = 1
                    external = int(bool(policy.get("external")))
        record = self.refresh(orchestration_id)
        if outcomes:
            record["last_execution_at"] = self._now()
            self._event(record, "campaign_continued", "Human", transitions=transitions,
                        external_operations=external, outcomes=outcomes)
            self._save(record)
        result = self.refresh(orchestration_id)
        result["execution"] = {"outcomes": outcomes, "transitions": transitions,
                               "external_operations": external, "limits": deepcopy(self.limits)}
        return result

    def advance_item(self, orchestration_id: str, work_item_id: str,
                     actor: str = "Human") -> dict[str, Any]:
        record = self.refresh(orchestration_id)
        item = next((value for value in record.get("work_item_states", [])
                     if value["work_item_id"] == work_item_id), None)
        if not item:
            raise KnowledgeCampaignOrchestrationError("Campaign work item was not found.")
        if item.get("action_authority") != "machine_safe":
            raise KnowledgeCampaignOrchestrationError("This item is at a human review gate or is not actionable.")
        outcome = self._execute(record["campaign_id"], work_item_id, item["next_action"])
        record = self.refresh(orchestration_id)
        record["last_execution_at"] = self._now()
        event_name = {
            "completed": "work_item_advanced",
            "package_reused": "package_reused",
            "failed": "work_item_advance_failed",
        }.get(outcome.get("status"), "work_item_advance_attempted")
        self._event(record, event_name, actor, work_item_id=work_item_id, outcome=outcome)
        self._save(record)
        result = self.refresh(orchestration_id)
        result["execution"] = {"outcomes": [outcome], "transitions": 1, "limits": deepcopy(self.limits)}
        return result

    def decide_command_relationship(
        self,
        campaign_id: str,
        work_item_id: str,
        decision: str,
        reason: str,
        reviewer: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Apply one exact campaign-owned command relationship decision atomically."""
        decision, reason, reviewer = decision.strip(), reason.strip(), reviewer.strip()
        if decision not in {"approve", "reject"}:
            raise KnowledgeCampaignOrchestrationError("Unsupported command relationship decision.")
        if not reason:
            raise KnowledgeCampaignOrchestrationError("A decision reason is required.")
        if not reviewer:
            raise KnowledgeCampaignOrchestrationError("An authenticated reviewer is required.")
        if not expected_fingerprint:
            raise KnowledgeCampaignOrchestrationError("The review fingerprint is required.")

        lock_path = self.campaign_root / ".command-relationship-decision.lock"
        with self._decision_lock(lock_path):
            # Local import avoids making the read-only projector depend on this writer.
            from app.services.review_workspace_service import ReviewWorkspaceService

            review = ReviewWorkspaceService(self.repository_root)
            item = review.find("command_relationship_review", work_item_id)
            if (
                item is None
                or item.get("source_fingerprint") != expected_fingerprint
                or item.get("technical", {}).get("Campaign") != campaign_id
            ):
                raise KnowledgeCampaignOrchestrationError(
                    "The command relationship review changed. Reload it before deciding."
                )

            campaign_path = self.planner._path(campaign_id)
            orchestrations = [
                value for value in self.read_persisted(self.campaign_root)
                if value.get("campaign_id") == campaign_id
            ]
            if len(orchestrations) != 1:
                raise KnowledgeCampaignOrchestrationError(
                    "The campaign orchestration is missing or ambiguous."
                )
            orchestration = orchestrations[0]
            orchestration_id = str(orchestration.get("orchestration_id") or "")
            orchestration_path = self._path(orchestration_id)
            campaign = self.planner.get(campaign_id)
            works = [value for value in campaign.get("work_items") or [] if (
                isinstance(value, dict)
                and value.get("work_item_id") == work_item_id
                and value.get("work_type") == "command_reference"
            )]
            states = [value for value in orchestration.get("work_item_states") or [] if (
                isinstance(value, dict) and value.get("work_item_id") == work_item_id
            )]
            if len(works) != 1 or len(states) != 1 or not (
                states[0].get("state") == "awaiting_human_review"
                and states[0].get("action_authority") == "human_gate"
                and states[0].get("next_action") == "review_command_reference"
            ):
                raise KnowledgeCampaignOrchestrationError(
                    "The command relationship is no longer at its human review gate."
                )
            work = works[0]
            handoff = work.get("command_relationship_review_handoff")
            if not isinstance(handoff, dict) or handoff.get("review_item_key") != item.get("key"):
                raise KnowledgeCampaignOrchestrationError(
                    "The command relationship handoff marker is invalid."
                )

            article_id = str(work.get("article_id") or "")
            command_id = str(work.get("command_identity") or "")
            article_path = self.repository_root / "knowledge_base" / "published" / f"{article_id}.json"
            command_path = self.repository_root / "knowledge_base" / "commands" / f"{command_id}.json"
            paths = (article_path, command_path, campaign_path, orchestration_path)
            try:
                before = {path: path.read_bytes() for path in paths}
                article = json.loads(before[article_path].decode("utf-8"))
                command = json.loads(before[command_path].decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise KnowledgeCampaignOrchestrationError(
                    f"Authoritative command relationship state could not be read: {error}"
                ) from error

            decided_at = self._now()
            decision_record = {
                "decision": decision,
                "reviewer": reviewer,
                "reason": reason,
                "decided_at": decided_at,
                "gap_identity": str(work.get("gap_identity") or ""),
                "source_fingerprint": expected_fingerprint,
                "relationship_evidence_fingerprint": str(
                    item.get("relationship_evidence_fingerprint") or ""
                ),
                "article_id": article_id,
                "command_identity": command_id,
            }
            replacements: dict[Path, bytes] = {}
            if decision == "approve":
                article_commands = article.setdefault("related_commands", [])
                command_articles = command.setdefault("related_articles", [])
                if not isinstance(article_commands, list) or not isinstance(command_articles, list):
                    raise KnowledgeCampaignOrchestrationError(
                        "Relationship declarations have an unsupported structure."
                    )
                if command_id not in article_commands:
                    article_commands.append(command_id)
                if article_id not in command_articles:
                    command_articles.append(article_id)
                replacements[article_path] = self._content_json_bytes(article)
                replacements[command_path] = self._content_json_bytes(command)

            work["status"] = "completed"
            work["command_relationship_decision"] = decision_record
            campaign.setdefault("history", []).append({
                "event": "command_relationship_decided",
                "at": decided_at,
                "actor": reviewer,
                "work_item_id": work_item_id,
                **decision_record,
            })
            replacements[campaign_path] = self._json_bytes(campaign)

            projected_states = deepcopy(orchestration.get("work_item_states") or [])
            target_states = [value for value in projected_states if (
                isinstance(value, dict) and value.get("work_item_id") == work_item_id
            )]
            if len(target_states) != 1:
                raise KnowledgeCampaignOrchestrationError(
                    "The command relationship orchestration state is ambiguous."
                )
            target_states[0].update(
                stage="command_reference_review_completed",
                state="complete",
                next_action=None,
                action_authority=None,
                review_link=None,
            )
            orchestration.update(self._projection(campaign, projected_states))
            self._event(
                orchestration,
                "command_relationship_decided",
                reviewer,
                work_item_id=work_item_id,
                decision=decision,
                reason=reason,
                article_id=article_id,
                command_identity=command_id,
            )
            orchestration.setdefault("fingerprints", {})["projection"] = self._fingerprint(
                self._projection(campaign, projected_states)
            )
            replacements[orchestration_path] = self._json_bytes(orchestration)

            written: list[Path] = []
            try:
                for path in paths:
                    if path.read_bytes() != before[path]:
                        raise KnowledgeCampaignOrchestrationError(
                            "Authoritative command relationship state changed during the decision."
                        )
                    if path in replacements:
                        self._atomic_write_bytes(path, replacements[path])
                        written.append(path)
                if decision == "approve":
                    saved_article = json.loads(article_path.read_text(encoding="utf-8"))
                    saved_command = json.loads(command_path.read_text(encoding="utf-8"))
                    if (
                        command_id not in (saved_article.get("related_commands") or [])
                        or article_id not in (saved_command.get("related_articles") or [])
                    ):
                        raise KnowledgeCampaignOrchestrationError(
                            "The reciprocal relationship could not be verified."
                        )
                saved_campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
                saved_orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
                saved_works = [value for value in saved_campaign.get("work_items") or [] if (
                    value.get("work_item_id") == work_item_id
                    and (value.get("command_relationship_decision") or {}).get("decision") == decision
                )]
                saved_states = [value for value in saved_orchestration.get("work_item_states") or [] if (
                    value.get("work_item_id") == work_item_id and value.get("state") == "complete"
                    and value.get("action_authority") is None and value.get("next_action") is None
                )]
                if len(saved_works) != 1 or len(saved_states) != 1:
                    raise KnowledgeCampaignOrchestrationError(
                        "The campaign human gate completion could not be verified."
                    )
                if review.find("command_relationship_review", work_item_id) is not None:
                    raise KnowledgeCampaignOrchestrationError(
                        "The campaign human gate did not close after the decision."
                    )
            except Exception as error:
                rollback_errors = []
                for path in reversed(written):
                    try:
                        if path.read_bytes() != replacements[path]:
                            rollback_errors.append(f"{path.name} changed concurrently")
                            continue
                        self._atomic_write_bytes(path, before[path])
                    except OSError as rollback_error:
                        rollback_errors.append(f"{path.name}: {rollback_error}")
                if rollback_errors:
                    raise KnowledgeCampaignOrchestrationError(
                        "Command relationship transaction failed and guarded rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    ) from error
                if isinstance(error, KnowledgeCampaignOrchestrationError):
                    raise
                raise KnowledgeCampaignOrchestrationError(
                    f"Command relationship transaction failed; all writes were restored: {error}"
                ) from error

            return {
                "campaign_id": campaign_id,
                "orchestration_id": orchestration_id,
                "work_item_id": work_item_id,
                "decision": decision,
                "article_id": article_id,
                "command_identity": command_id,
            }

    def _resolve_item(self, campaign, work):
        base = {"work_item_id": work["work_item_id"], "gap_id": work["gap_id"],
                "title": work.get("area_id", "").replace("-", " ").title(),
                "work_type": work.get("work_type"), "priority": work.get("priority", "medium"),
                "stage": "coverage_identified", "state": "ready", "next_action": None,
                "action_authority": None, "package_id": None, "review_link": None,
                "blocker": None, "dependencies": [], "stale": False}
        if campaign.get("status") == "draft" or not campaign.get("last_analyzed_at"):
            return self._action(base, "coverage_identified", "analyze_coverage")
        if work.get("work_type") == "command_reference" and isinstance(
            work.get("command_relationship_decision"), dict
        ) and work["command_relationship_decision"].get("decision") in {"approve", "reject"}:
            base.update(
                stage="command_reference_review_completed", state="complete",
                next_action=None, action_authority=None,
                review_link=None,
            )
            return base
        reuse = self._reuse_for_work(campaign, work)
        if reuse and work.get("work_type") not in WORKFLOW_TYPES:
            destination = self.review_destinations.resolve(reuse)
            if not destination.get("resolved"):
                return self._blocked(base, "reuse_target", "Reuse identity",
                                     destination.get("reason", "The reuse target could not be resolved."),
                                     "Reconcile the reuse opportunity with an existing governed resource.")
            base.update(stage="reuse_available", state="complete", next_action=None,
                        package_id=destination["resource_id"], dependencies=[destination["resource_id"]],
                        review_destination=destination,
                        reuse={"opportunity_id": reuse.get("opportunity_id"),
                               "article_id": reuse.get("article_id"),
                               "workflow_ids": list(reuse.get("workflow_ids") or [])})
            return base
        if work.get("work_type") == "learning_content":
            destination = self.review_destinations.resolve_learning_authoring(work)
            if not destination.get("resolved"):
                return self._blocked(
                    base,
                    "learning_authoring_target",
                    "Workflow authoring",
                    destination.get(
                        "reason", "The learning-authoring workflow could not be resolved."
                    ),
                    "Reconcile the campaign workflow identity before authoring learning content.",
                )
            base.update(
                review_destination=destination,
                review_link=f"/workflow-studio?workflow={destination['resource_id']}",
            )
            return self._gate(
                base, "learning_authoring_required", "author_learning_content"
            )
        if work.get("work_type") == "command_reference":
            command_id = str(work.get("command_identity") or "")
            base["review_link"] = f"/commands/{command_id}" if command_id else "/commands"
            return self._gate(
                base, "command_reference_review_required", "review_command_reference"
            )
        if work.get("work_type") in WORKFLOW_TYPES:
            return self._resolve_workflow(campaign, work, base)
        return self._resolve_article(campaign, work, base)

    @staticmethod
    def _reuse_for_work(campaign, work):
        candidates = list(campaign.get("reuse_opportunities") or [])
        explicit = work.get("reuse_opportunity_id")
        if explicit:
            return next((item for item in candidates if item.get("opportunity_id") == explicit), None)
        target = work.get("target_asset")
        if target:
            return next((item for item in candidates if target in {
                item.get("article_id"), item.get("workflow_id"), item.get("target_asset")}), None)
        evidence = set(work.get("evidence") or [])
        evidence_matches = [item for item in candidates if evidence.intersection(item.get("evidence") or [])]
        if len(evidence_matches) == 1:
            return evidence_matches[0]
        area_matches = [item for item in candidates if work.get("area_id") in (item.get("areas") or [])]
        return area_matches[0] if len(area_matches) == 1 else None

    def _resolve_workflow(self, campaign, work, base):
        packages = [item for item in self.workflows.list_for_campaign(campaign["campaign_id"])
                    if item.get("work_item_id") == work["work_item_id"]]
        if not packages:
            eligibility = self.workflows.eligibility(campaign["campaign_id"], work["work_item_id"])
            if not eligibility.get("eligible"):
                return self._resolve_workflow_claims(campaign, work, base, eligibility)
            return self._action(base, "workflow_planning_ready", "prepare_workflow_package")
        package = packages[0]
        base.update(package_id=package["generation_id"], dependencies=[package["generation_id"]],
                    review_link=f"/curator/growth/workflow-generation/{package['generation_id']}")
        status = package.get("effective_status") or package.get("status")
        if status == "stale":
            return self._blocked(base, "stale", "Workflow generation", "Upstream inputs changed.", "Refresh the workflow package.", stale=True)
        if status == "prepared": return self._action(base, "workflow_planning_ready", "plan_workflow")
        if status == "plan_ready": return self._action(base, "workflow_draft_ready", "prepare_workflow_draft")
        if status == "draft_ready": return self._gate(base, "draft_review_required", "review_workflow_draft")
        if status == "approved_for_handoff": return self._gate(base, "content_studio_ready", "accept_workflow_content_studio")
        if status == "handed_off":
            base.update(stage="content_studio_ready", state="complete", next_action=None)
            return base
        if status in {"needs_revision", "rejected"}:
            return self._blocked(base, "workflow_validation", "Workflow generation", "Workflow draft requires revision.", "Review the workflow package.")
        return self._blocked(base, "workflow_state", "Workflow generation", f"Workflow package is in '{status}'.", "Review the authoritative package.")

    def _resolve_workflow_claims(self, campaign, work, base, eligibility):
        research = [item for item in self.research.list_for_campaign(campaign["campaign_id"])
                    if item.get("work_item_id") == work["work_item_id"]]
        if not research:
            return self._action(base, "research_needed", "prepare_research")
        rp = research[0]
        base.update(package_id=rp["package_id"], dependencies=[rp["package_id"]],
                    review_link=f"/curator/growth/source-research/{rp['package_id']}")
        if rp.get("status") in {"pending", "researching"}:
            return self._action(base, "research_needed", "run_source_research")
        if rp.get("status") == "ready_for_review":
            return self._gate(base, "source_approval_required", "approve_source")
        if rp.get("status") != "approved":
            return self._blocked(base, "source_state", "Source research",
                                 f"Research is {rp.get('status')}.", "Review or refresh the research package.",
                                 stale=rp.get("status") == "needs_refresh")
        selected = list(rp.get("selected_sources") or [])
        extractions = self.evidence.list_for_research(rp["package_id"])
        self._add_evidence_progress(base, selected, extractions)
        missing = next((source for source in selected if not any(
            item.get("source_candidate_id") == source for item in extractions)), None)
        if missing:
            base["source_candidate_id"] = missing
            return self._action(base, "evidence_extraction_ready", "prepare_evidence")
        proposed = next((item for item in extractions if item.get("status") == "proposed"), None)
        if proposed:
            base.update(package_id=proposed["extraction_id"],
                        dependencies=base["dependencies"] + [proposed["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{proposed['extraction_id']}")
            return self._action(base, "evidence_extraction_ready", "extract_evidence")
        pending = next((item for item in extractions if item.get("status") in {
            "retrieving", "needs_review", "partially_approved", "extracted"
        }), None)
        if pending:
            base.update(package_id=pending["extraction_id"],
                        dependencies=base["dependencies"] + [pending["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{pending['extraction_id']}")
            return self._gate(base, "evidence_review_required", "review_evidence")
        insufficient = next((item for item in extractions
                             if item.get("status") == "insufficient_evidence"), None)
        approved = any(item.get("status") == "approved" for item in extractions)
        if insufficient and not approved:
            base.update(package_id=insufficient["extraction_id"],
                        dependencies=base["dependencies"] + [insufficient["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{insufficient['extraction_id']}")
            result = self._blocked(
                base, "insufficient_evidence", "Evidence research",
                "Human review confirmed that the extracted source contains no Candidate Evidence.",
                "Select or approve another authoritative source, or initiate governed follow-up research."
            )
            result["stage"] = "insufficient_evidence"
            return result
        plans = self.claims.list_for_work(campaign["campaign_id"], work["work_item_id"])
        if not plans:
            return self._action(base, "workflow_claim_planning_ready", "prepare_workflow_claim_plan")
        plan = plans[0]
        base.update(package_id=plan["claim_plan_id"],
                    dependencies=base["dependencies"] + [plan["claim_plan_id"]],
                    review_link=f"/curator/growth/claim-planning/{plan['claim_plan_id']}")
        if plan.get("status") == "proposed":
            return self._action(base, "workflow_claim_planning_ready", "plan_workflow_claims")
        if plan.get("status") in {"needs_review", "partially_approved"}:
            return self._gate(base, "workflow_claim_review_required", "review_claims")
        reasons = " ".join(str(item) for item in eligibility.get("reasons") or [])
        return self._blocked(base, "workflow_eligibility", "Workflow generation",
                             reasons or f"Workflow claim plan is {plan.get('status')}.",
                             "Resolve evidence, conflicts, or claim review in the workflow-claim workspace.",
                             stale=plan.get("status") == "needs_evidence")

    def _resolve_article(self, campaign, work, base):
        research = [item for item in self.research.list_for_campaign(campaign["campaign_id"])
                    if item.get("work_item_id") == work["work_item_id"]]
        if not research:
            return self._action(base, "research_needed", "prepare_research")
        rp = research[0]
        base.update(package_id=rp["package_id"], dependencies=[rp["package_id"]],
                    review_link=f"/curator/growth/source-research/{rp['package_id']}")
        if rp.get("status") in {"pending", "researching"}:
            return self._action(base, "research_needed", "run_source_research")
        if rp.get("status") == "ready_for_review":
            return self._gate(base, "source_approval_required", "approve_source")
        if rp.get("status") in {"needs_refresh", "rejected", "archived"}:
            return self._blocked(base, "source_state", "Source research", f"Research is {rp.get('status') }.", "Review or refresh the research package.", stale=rp.get("status") == "needs_refresh")
        if rp.get("status") != "approved":
            return self._blocked(base, "source_state", "Source research", "Research has not reached an approved state.", "Review the research package.")
        selected = list(rp.get("selected_sources") or [])
        extractions = self.evidence.list_for_research(rp["package_id"])
        self._add_evidence_progress(base, selected, extractions)
        missing = next((source for source in selected if not any(item.get("source_candidate_id") == source for item in extractions)), None)
        if missing:
            base["source_candidate_id"] = missing
            return self._action(base, "evidence_extraction_ready", "prepare_evidence")
        proposed = next((item for item in extractions if item.get("status") == "proposed"), None)
        if proposed:
            base.update(package_id=proposed["extraction_id"], dependencies=base["dependencies"] + [proposed["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{proposed['extraction_id']}")
            return self._action(base, "evidence_extraction_ready", "extract_evidence")
        stale = next((item for item in extractions if item.get("status") in {"needs_refresh", "failed"}), None)
        if stale:
            base.update(package_id=stale["extraction_id"], dependencies=base["dependencies"] + [stale["extraction_id"]])
            return self._blocked(base, "evidence_state", "Evidence extraction", f"Evidence is {stale.get('status')}.", "Refresh or inspect the evidence package.", stale=stale.get("status") == "needs_refresh")
        pending = next((item for item in extractions if item.get("status") in {"retrieving", "needs_review", "partially_approved", "extracted"}), None)
        if pending:
            base.update(package_id=pending["extraction_id"], dependencies=base["dependencies"] + [pending["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{pending['extraction_id']}")
            return self._gate(base, "evidence_review_required", "review_evidence")
        insufficient = next((item for item in extractions
                             if item.get("status") == "insufficient_evidence"), None)
        approved = any(item.get("status") == "approved" for item in extractions)
        if insufficient and not approved:
            base.update(package_id=insufficient["extraction_id"],
                        dependencies=base["dependencies"] + [insufficient["extraction_id"]],
                        review_link=f"/curator/growth/evidence-extraction/{insufficient['extraction_id']}")
            result = self._blocked(
                base, "insufficient_evidence", "Evidence research",
                "Human review confirmed that the extracted source contains no Candidate Evidence.",
                "Select or approve another authoritative source, or initiate governed follow-up research."
            )
            result["stage"] = "insufficient_evidence"
            return result
        drafts = [item for item in self.generation.list_for_campaign(campaign["campaign_id"])
                  if item.get("work_item_id") == work["work_item_id"]]
        if not drafts:
            return self._action(base, "claim_planning_ready", "prepare_article_package")
        draft = drafts[0]
        base.update(package_id=draft["package_id"], dependencies=base["dependencies"] + [draft["package_id"]],
                    review_link=f"/curator/growth/draft-generation/{draft['package_id']}")
        plans = self.claims.list_for_kdg(draft["package_id"])
        if not plans:
            return self._action(base, "claim_planning_ready", "prepare_claim_plan")
        plan = plans[0]
        base["dependencies"].append(plan["claim_plan_id"])
        base["review_link"] = f"/curator/growth/claim-planning/{plan['claim_plan_id']}"
        if plan.get("status") == "proposed": return self._action(base, "claim_planning_ready", "plan_claims")
        if plan.get("status") in {"planned", "needs_review", "partially_approved"}:
            return self._gate(base, "claim_review_required", "review_claims")
        if plan.get("status") in {"needs_evidence", "conflicted", "rejected", "superseded"}:
            return self._blocked(base, "claim_state", "Claim planning", f"Claim plan is {plan.get('status')}.", "Resolve evidence gaps or conflicts in the claim workspace.", stale=plan.get("status") == "needs_evidence")
        assemblies = self.assembly.list_for_kdg(draft["package_id"])
        if plan.get("status") == "ready_for_drafting" and not assemblies:
            return self._action(base, "article_assembly_ready", "assemble_article")
        assembly = assemblies[0] if assemblies else None
        if assembly:
            base.update(package_id=assembly["assembly_id"], dependencies=base["dependencies"] + [assembly["assembly_id"]],
                        review_link=f"/curator/growth/draft-assembly/{assembly['assembly_id']}")
            status = assembly.get("status")
            if status == "ready_for_review": return self._gate(base, "draft_review_required", "review_article_draft")
            if status == "handed_off":
                base.update(stage="content_studio_ready", state="complete", next_action=None)
                return base
            return self._blocked(base, "assembly_state", "Draft assembly", f"Assembly is {status}.", "Review the assembly and validation results.", stale=status == "stale")
        return self._blocked(base, "article_state", "Article pipeline", "No eligible article action is currently available.", "Review the authoritative packages.")

    def _execute(self, campaign_id, work_item_id, action):
        try:
            if action == "analyze_coverage":
                self.planner.analyze(campaign_id)
                return {"work_item_id": work_item_id, "action": action, "status": "completed"}
            campaign = self.planner.get(campaign_id)
            work = next(item for item in campaign.get("work_items", []) if item["work_item_id"] == work_item_id)
            if action == "prepare_research": self.research.create(campaign_id, work["gap_id"], work_item_id)
            elif action == "run_source_research":
                package = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.research.run(package["package_id"])
            elif action == "prepare_evidence":
                package = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                existing = self.evidence.list_for_research(package["package_id"])
                missing = next((source for source in package["selected_sources"] if not any(
                    item.get("source_candidate_id") == source for item in existing)), None)
                if missing is None:
                    prepared = next((item for source in reversed(package["selected_sources"])
                                     for item in existing
                                     if item.get("source_candidate_id") == source), None)
                    return {
                        "work_item_id": work_item_id,
                        "action": action,
                        "status": "package_reused",
                        "source_candidate_id": (prepared or {}).get("source_candidate_id"),
                        "extraction_id": (prepared or {}).get("extraction_id"),
                        "package_disposition": "reused",
                    }
                prepared = self.evidence.prepare(package["package_id"], missing)
                reused = any(item.get("extraction_id") == prepared.get("extraction_id") for item in existing)
                return {
                    "work_item_id": work_item_id,
                    "action": action,
                    "status": "package_reused" if reused else "completed",
                    "source_candidate_id": missing,
                    "extraction_id": prepared.get("extraction_id"),
                    "package_disposition": "reused" if reused else "created",
                }
            elif action == "extract_evidence":
                rp = next(item for item in self.research.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                package = next(item for item in self.evidence.list_for_research(rp["package_id"]) if item.get("status") == "proposed")
                self.evidence.extract(package["extraction_id"])
            elif action == "prepare_article_package": self.generation.prepare(campaign_id, work["gap_id"], work_item_id)
            elif action == "prepare_claim_plan":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.claims.prepare(package["package_id"])
            elif action == "plan_claims":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.claims.plan(self.claims.list_for_kdg(package["package_id"])[0]["claim_plan_id"])
            elif action == "prepare_workflow_claim_plan":
                self.claims.prepare_workflow(campaign_id, work_item_id)
            elif action == "plan_workflow_claims":
                plan = self.claims.list_for_work(campaign_id, work_item_id)[0]
                self.claims.plan(plan["claim_plan_id"])
            elif action == "assemble_article":
                package = next(item for item in self.generation.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.assembly.assemble(self.claims.list_for_kdg(package["package_id"])[0]["claim_plan_id"])
            elif action == "prepare_workflow_package": self.workflows.prepare(campaign_id, work_item_id)
            elif action == "plan_workflow":
                package = next(item for item in self.workflows.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.workflows.plan(package["generation_id"])
            elif action == "prepare_workflow_draft":
                package = next(item for item in self.workflows.list_for_campaign(campaign_id) if item["work_item_id"] == work_item_id)
                self.workflows.prepare_draft(package["generation_id"])
            else: raise KnowledgeCampaignOrchestrationError(f"Action '{action}' is not machine-safe.")
            return {"work_item_id": work_item_id, "action": action, "status": "completed"}
        except Exception as error:  # isolate one phase failure from independent work
            return {"work_item_id": work_item_id, "action": action, "status": "failed",
                    "error_type": type(error).__name__, "message": str(error), "retry_eligible": False,
                    "at": self._now()}

    @staticmethod
    def _add_evidence_progress(base, selected_sources, extractions):
        """Add read-only per-source preparation detail to a work-item projection."""
        packages_by_source = {}
        for package in extractions:
            source_id = package.get("source_candidate_id")
            extraction_id = package.get("extraction_id")
            if source_id in selected_sources and extraction_id and source_id not in packages_by_source:
                packages_by_source[source_id] = {
                    "source_candidate_id": source_id,
                    "extraction_id": extraction_id,
                    "status": package.get("status", "unknown"),
                    "review_link": f"/curator/growth/evidence-extraction/{extraction_id}",
                }
        base["evidence_progress"] = {
            "prepared_sources": len(packages_by_source),
            "total_sources": len(selected_sources),
            "remaining_sources": max(0, len(selected_sources) - len(packages_by_source)),
        }
        base["evidence_packages"] = [
            packages_by_source[source_id]
            for source_id in selected_sources
            if source_id in packages_by_source
        ]

    def _projection(self, campaign, states):
        unique = []
        seen = set()
        for item in states:
            identity = (item.get("work_item_id"), (item.get("reuse") or {}).get("opportunity_id"),
                        item.get("stage"), item.get("package_id"))
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        states = unique
        actionable = [item for item in states if item.get("action_authority") == "machine_safe"]
        if (campaign.get("status") == "draft" or not campaign.get("last_analyzed_at")) and not actionable:
            actionable.append({
                "work_item_id": "__campaign__", "gap_id": None,
                "title": "Analyze campaign coverage", "work_type": "campaign",
                "priority": "medium", "stage": "coverage_identified",
                "state": "machine_ready", "next_action": "analyze_coverage",
                "action_authority": "machine_safe", "package_id": None,
                "review_link": None, "blocker": None, "dependencies": [], "stale": False,
            })
        review = [self._review_entry(item) for item in states if item.get("action_authority") == "human_gate"]
        blockers = [item["blocker"] for item in states if item.get("blocker")]
        stale = [self._stale_entry(item) for item in states if item.get("stale")]
        completed = [item["work_item_id"] for item in states if item.get("state") == "complete"]
        counts = {key: sum(1 for item in states if item.get("stage") == key) for key in {
            "coverage_identified", "research_needed", "source_approval_required", "evidence_extraction_ready",
            "evidence_review_required", "claim_planning_ready", "claim_review_required", "article_assembly_ready",
            "workflow_claim_planning_ready", "workflow_claim_review_required",
            "workflow_planning_ready", "workflow_draft_ready", "draft_review_required", "content_studio_ready", "blocked", "stale"}}
        total = len(states)
        status = "completed" if total and len(completed) == total and not blockers and not stale else (
            "awaiting_human_review" if review else "blocked" if blockers and not actionable else "active")
        next_action = (actionable[0] if actionable else review[0] if review else blockers[0] if blockers else None)
        return {
            "campaign_objective": campaign.get("objective", ""), "status": status,
            "work_item_states": states, "actionable_queue": actionable, "human_review_queue": review,
            "blockers": blockers, "stale_dependencies": stale, "completed_items": completed,
            "next_recommended_action": next_action, "pipeline_summary": counts,
            "readiness_summary": {"total": total, "completed": len(completed), "machine_ready": len(actionable),
                                  "human_review": len(review), "blocked": len(blockers), "stale": len(stale),
                                  "content_studio_ready": sum(1 for item in states if item.get("stage") == "content_studio_ready"),
                                  "completion_percent": round(100 * len(completed) / total) if total else 0},
            "dependency_graph": self._graph(campaign, states),
        }

    @staticmethod
    def _graph(campaign, states):
        nodes = [{"id": campaign["campaign_id"], "type": "campaign", "label": campaign.get("title", "Campaign")}]
        edges = []
        for item in states:
            nodes.append({"id": item["work_item_id"], "type": "work_item", "label": item["title"]})
            edges.append({"from": campaign["campaign_id"], "to": item["work_item_id"]})
            parent = item["work_item_id"]
            for dependency in item.get("dependencies", []):
                nodes.append({"id": dependency, "type": dependency.split("-", 1)[0], "label": dependency})
                edges.append({"from": parent, "to": dependency})
                parent = dependency
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _action(base, stage, action):
        base.update(stage=stage, state="machine_ready", next_action=action,
                    action_authority=ACTION_POLICY[action]["authority"])
        return base

    @staticmethod
    def _gate(base, stage, action):
        base.update(stage=stage, state="awaiting_human_review", next_action=action,
                    action_authority=ACTION_POLICY[action]["authority"])
        return base

    def _blocked(self, base, kind, subsystem, explanation, resolution, stale=False):
        base.update(stage="stale" if stale else "blocked", state="blocked", next_action=None,
                    action_authority=None, stale=stale, blocker={
                        "blocker_type": kind, "work_item_id": base["work_item_id"], "originating_subsystem": subsystem,
                        "original_package_id": base.get("package_id"), "severity": "medium",
                        "explanation": explanation, "recommended_resolution": resolution, "timestamp": self._now()})
        return base

    @staticmethod
    def _review_entry(item):
        return {"work_item_id": item["work_item_id"], "title": item["title"], "phase": item["stage"],
                "action": item["next_action"], "why": "A governed approval boundary has been reached.",
                "risk": item.get("priority", "medium"), "provenance": item.get("package_id"),
                "review_link": item.get("review_link"),
                "review_destination": deepcopy(item.get("review_destination"))}

    @staticmethod
    def _stale_entry(item):
        return {"work_item_id": item["work_item_id"], "changed": item.get("package_id"),
                "downstream_impact": list(item.get("dependencies") or []),
                "explanation": item.get("blocker", {}).get("explanation")}

    def _path(self, orchestration_id):
        if not orchestration_id.startswith("KORCH-") or not orchestration_id[6:].isalnum():
            raise KnowledgeCampaignOrchestrationError("Invalid orchestration ID.")
        return self.package_root / f"{orchestration_id}.json"

    def _save(self, record):
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(record["orchestration_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _content_json_bytes(value: dict[str, Any]) -> bytes:
        """Serialize authoritative content without reordering its existing keys."""
        return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False, suffix=".tmp") as output:
                temporary_name = output.name
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    @contextmanager
    def _decision_lock(
        path: Path, timeout: float = 2.0,
        operation: str = "command relationship decision",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        descriptor = None
        while descriptor is None:
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise KnowledgeCampaignOrchestrationError(
                        f"Another {operation} is in progress."
                    )
                time.sleep(0.02)
            except OSError as error:
                raise KnowledgeCampaignOrchestrationError(
                    f"The {operation} lock is unavailable: {error}"
                ) from error
        try:
            os.write(descriptor, json.dumps({"pid": os.getpid()}).encode("utf-8"))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)

    @staticmethod
    def _read(path):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeCampaignOrchestrationError(f"Unable to read orchestration state: {error}") from error

    @staticmethod
    def _stable_id(prefix, *parts):
        return f"{prefix}-{hashlib.sha256('|'.join(str(item) for item in parts).encode()).hexdigest()[:12].upper()}"

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _now(): return datetime.now(timezone.utc).isoformat()

    def _event(self, record, event, actor, **details):
        record.setdefault("history", []).append({"event": event, "at": self._now(), "actor": actor, **details})
        record["updated_at"] = self._now()
