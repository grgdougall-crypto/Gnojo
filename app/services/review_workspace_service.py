from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from secrets import compare_digest
from typing import Any
from urllib.parse import quote, urlencode

from app.repositories.knowledge_repository import (
    ArticleNotFoundError,
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from app.services.curator_growth_service import CuratorGrowthService
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from app.services.curator_task_service import CuratorTaskService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.knowledge_campaign_orchestration_service import (
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)
from curator.calibration import ReasoningCalibrationService
from curator.memory import CuratorMemoryError
from curator.workflow_reasoning import WorkflowReasoningAuditor


class ReviewBatchError(RuntimeError):
    """A governed batch preview or commit failed closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ReviewWorkspaceService:
    """Project existing Curator and Growth queues into one read-only review view."""

    ACTIONABLE_TASK_STATUSES = frozenset({"open", "in_progress", "deferred"})
    CORRECTED_VERIFICATION_STATUSES = frozenset({
        "appears_corrected", "corrected", "relationship_satisfied", "satisfied",
    })

    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.tasks = CuratorTaskService(self.root)
        self.growth = CuratorGrowthService(self.root)

    def workspace(self, selected: str = "") -> dict[str, Any]:
        items = self.items()
        current = next((item for item in items if item["key"] == selected), None)
        if current is None and items:
            current = items[0]
        next_item = self.next_item(items, current["key"] if current else "")
        return {
            "items": items,
            "current": current,
            "next": next_item,
            "remaining": len(items),
            "compression_summary": dict(Counter(
                item["compression"]["classification"] for item in items
            )),
        }

    def items(self) -> list[dict[str, Any]]:
        state = self.tasks.store.load()
        task_records = list(state.get("tasks", {}).values())
        projected: list[dict[str, Any]] = []
        for raw in task_records:
            if raw.get("status") not in self.ACTIONABLE_TASK_STATUSES:
                continue
            projected.append(self._task_item(raw))

        growth = self.growth.dashboard()
        lessons = list(growth.get("lessons", []))
        proposals = list(growth.get("proposals", []))
        projected.extend(
            self._lesson_item(item) for item in lessons
            if item.get("status") == "proposed"
        )
        projected.extend(
            self._proposal_item(item) for item in proposals
            if item.get("kind") == "capability" and item.get("status") == "proposed"
        )
        projected.extend(self._command_relationship_items())
        self._apply_compression(projected, task_records, lessons, proposals)
        ordered = sorted(projected, key=lambda item: (item["order_group"], item["key"]))
        for item in ordered:
            item["queue_reason"] = self._queue_reason(item)
        return ordered

    def find(self, item_type: str, item_id: str) -> dict[str, Any] | None:
        return next((item for item in self.items()
                     if item["item_type"] == item_type and item["item_id"] == item_id), None)

    @staticmethod
    def command_relationship_review_key(work_item_id: Any) -> str:
        """Return the canonical Review identity for a valid campaign work item."""
        identity = str(work_item_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity):
            return ""
        return f"command_relationship_review:{identity}"

    def batch_preview(self, item_type: str, item_id: str) -> dict[str, Any]:
        """Return a read-only, exact snapshot of one eligible routine group."""
        items = self.items()
        selected = next((item for item in items if item["item_type"] == item_type
                         and item["item_id"] == item_id), None)
        if selected is None:
            raise ReviewBatchError("changed", "The selected review item is no longer actionable.")
        if selected["compression"]["classification"] != "Routine pattern":
            raise ReviewBatchError(
                "unsupported", "Only Routine-pattern items with consistent precedent can be reviewed as a group."
            )
        if item_type not in {"curator_task", "growth_lesson"}:
            raise ReviewBatchError(
                "unsupported", "This review item does not support governed group decisions."
            )

        matching = [item for item in items if item.get("batch_group_key") == selected.get("batch_group_key")]
        candidates, excluded = [], []
        for item in matching:
            reason = self._batch_exclusion_reason(item)
            if reason:
                excluded.append({"item": item, "reason": reason})
            else:
                candidates.append(item)
        if len(candidates) < 2 or selected not in candidates:
            raise ReviewBatchError(
                "unsupported", "Fewer than two currently eligible items remain in this routine group."
            )

        common_actions = self._common_batch_actions(candidates)
        if not common_actions:
            raise ReviewBatchError(
                "unsupported", "The current items do not share a compatible authoritative decision."
            )
        snapshot = {
            "schema_version": 1,
            "item_type": item_type,
            "group_key": selected["batch_group_key"],
            "candidates": [{
                "item_id": item["item_id"],
                "finding_id": item.get("finding_id", ""),
                "source_fingerprint": item["source_fingerprint"],
                "status": item["authoritative_status"],
                "allowed_actions": list(item["batch_allowed_actions"]),
            } for item in candidates],
            "excluded": [{
                "item_id": entry["item"]["item_id"],
                "source_fingerprint": entry["item"]["source_fingerprint"],
                "reason": entry["reason"],
            } for entry in excluded],
            "actions": list(common_actions),
            "precedent": {
                "classification": selected["compression"]["classification"],
                "prior_dispositions": selected["compression"]["prior_dispositions"],
                "basis": selected["compression"]["basis"],
            },
        }
        return {
            "selected": selected,
            "pattern": selected["compression"],
            "candidates": candidates,
            "excluded": excluded,
            "actions": common_actions,
            "snapshot_fingerprint": self._fingerprint(snapshot),
            "notice": "No decision has been applied yet.",
        }

    def commit_batch(self, item_type: str, item_id: str, *, snapshot_fingerprint: str,
                     decision: str, reason: str, reviewer: str) -> dict[str, Any]:
        """Apply a validated group through existing actions and one atomic memory CAS."""
        if not reason.strip():
            raise ReviewBatchError("invalid", "A decision reason is required.")
        if not reviewer.strip():
            raise ReviewBatchError("invalid", "A human reviewer is required.")

        with self.tasks.store.locked() as memory:
            before = memory.snapshot()
            try:
                preview = self.batch_preview(item_type, item_id)
            except ReviewBatchError as error:
                raise ReviewBatchError(
                    "changed",
                    "This group changed after preview. No decisions were saved; review the refreshed group.",
                ) from error
            if not snapshot_fingerprint or not compare_digest(
                    preview["snapshot_fingerprint"], snapshot_fingerprint):
                raise ReviewBatchError(
                    "changed", "This group changed after preview. No decisions were saved; review the refreshed group."
                )
            if decision not in preview["actions"]:
                raise ReviewBatchError(
                    "changed", "That decision is no longer valid for every item in this group. No decisions were saved."
                )

            with tempfile.TemporaryDirectory(prefix="gnojo-review-batch-") as directory:
                shadow_root = Path(directory)
                shadow_store = self.tasks.store.__class__(shadow_root / "curation_memory")
                # Strip CuratorMemoryState's original-repository CAS fingerprint;
                # the shadow repository is intentionally new and absent.
                shadow_store.save(deepcopy(dict(before.state)))
                try:
                    for item in preview["candidates"]:
                        self._apply_existing_action(
                            shadow_root, item_type, item["item_id"], decision,
                            reason.strip(), reviewer.strip(),
                        )
                except (CuratorMemoryError, ValueError, RuntimeError) as error:
                    raise ReviewBatchError(
                        "invalid", f"The batch could not be applied atomically: {error}"
                    ) from error
                after = shadow_store.load()
                memory.compare_and_swap(before.fingerprint, after)

        return {"count": len(preview["candidates"]), "item_ids": [
            item["item_id"] for item in preview["candidates"]
        ]}

    @staticmethod
    def _apply_existing_action(root: Path, item_type: str, item_id: str, decision: str,
                               reason: str, reviewer: str) -> None:
        if item_type == "curator_task":
            action = {
                "resolve": "resolve", "resolve_verified": "resolve",
                "defer": "defer", "ignore": "ignore",
            }.get(decision)
            if not action:
                raise CuratorMemoryError("Unsupported Knowledge Task batch decision.")
            CuratorTaskService(root).update(item_id, action=action, note=reason)
            return
        if item_type == "growth_lesson":
            status = {"approve": "approved", "reject": "rejected"}.get(decision)
            if not status:
                raise ValueError("Unsupported Growth lesson batch decision.")
            CuratorGrowthService(root).decide(
                "lesson", item_id, status, reviewer=reviewer, reason=reason,
            )
            return
        raise ValueError("Unsupported governed batch item type.")

    @staticmethod
    def _batch_exclusion_reason(item: dict[str, Any]) -> str:
        if item["compression"]["classification"] != "Routine pattern":
            return "The item's precedent is no longer a consistent Routine pattern."
        if item["item_type"] == "growth_lesson":
            return "" if item["authoritative_status"] == "proposed" else "The lesson is no longer proposed."
        if item["item_type"] != "curator_task":
            return "This item type is not supported by governed group review."
        if item["authoritative_status"] not in ReviewWorkspaceService.ACTIONABLE_TASK_STATUSES:
            return "The Knowledge Task is no longer actionable."
        if not item.get("finding_id") or not item.get("affected_identity"):
            return "The task does not have an unambiguous authoritative identity."
        if item.get("specialized_review"):
            return "This task requires its specialized supervised review path."
        return ""

    @staticmethod
    def _common_batch_actions(candidates: list[dict[str, Any]]) -> tuple[str, ...]:
        if not candidates:
            return ()
        common = set(candidates[0]["batch_allowed_actions"])
        for item in candidates[1:]:
            common.intersection_update(item["batch_allowed_actions"])
        if "resolve_verified" in common:
            common.discard("resolve")
        order = ("resolve_verified", "resolve", "defer", "ignore", "approve", "reject")
        return tuple(action for action in order if action in common)

    @staticmethod
    def next_item(items: list[dict[str, Any]], current_key: str) -> dict[str, Any] | None:
        if not items:
            return None
        position = next((index for index, item in enumerate(items)
                         if item["key"] == current_key), -1)
        if position < 0:
            return items[0]
        return items[position + 1] if position + 1 < len(items) else None

    def _task_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        task_id = str(raw.get("task_id") or "")
        key = f"curator_task:{task_id}"
        return_to = "/review?" + urlencode({"item": key})
        try:
            task = self.tasks.get(
                task_id, origin="review_workspace", return_to=return_to,
            )
        except (CuratorMemoryError, OSError, ValueError):
            task = dict(raw)
        current = task.get("current_content") or {}
        verification = task.get("current_verification") or {}
        related = task.get("related_tasks") or []
        calibration = task.get("calibration_context") or {}
        affected = str(task.get("content_identifier") or "")
        article = self._article_context(task)
        content_label = str(
            current.get("title") or article.get("title") or affected or "Affected content"
        )
        inspect_url = (task.get("navigation") or {}).get("url", "")
        if not inspect_url:
            inspect_url = f"/curator/tasks/{quote(task_id, safe='')}?" + urlencode({
                "origin": "review_workspace", "return_to": return_to,
            })
        item = {
            "key": key,
            "item_type": "curator_task",
            "type_label": "Knowledge Task",
            "item_id": task_id,
            "finding_id": str(task.get("finding_id") or ""),
            "title": str(task.get("title") or content_label),
            "summary": str(task.get("explanation") or "Curator identified an item that needs human review."),
            "priority": str(task.get("priority") or ""),
            "risk": str(task.get("classification") or ""),
            "confidence": str(task.get("confidence") or ""),
            "affected_content": content_label,
            "affected_identity": affected,
            "current_state": str(
                verification.get("status") or article.get("state")
                or task.get("status") or "Not verified"
            ),
            "current_detail": str(
                verification.get("message") or current.get("instruction")
                or article.get("preview")
                or "Current content has not been explicitly verified from this workspace."
            ),
            "recommendation": str(task.get("recommended_action") or "Review the finding and current content."),
            "impact": str(task.get("guidance", {}).get("impact") or
                          f"Knowledge debt: {task.get('knowledge_debt_score', 0)}"),
            "precedent": self._task_precedent(related, calibration, task),
            "inspect_url": inspect_url,
            "task_url": f"/curator/tasks/{quote(task_id, safe='')}?" + urlencode({
                "origin": "review_workspace", "return_to": return_to,
            }),
            "return_to": return_to,
            "allowed_actions": ("resolve", "defer", "ignore", "verify"),
            "authoritative_status": str(raw.get("status") or ""),
            "resolve_verified": self._fresh_corrected(task),
            "affected_fingerprint": str(task.get("affected_fingerprint") or ""),
            "source_fingerprint": self._fingerprint({
                "record": raw,
                "affected_fingerprint": str(task.get("affected_fingerprint") or ""),
            }),
            "technical": {
                "Rule": str(task.get("curator_rule") or ""),
                "Finding": str(task.get("finding_id") or ""),
                "Task": task_id,
                "Evidence items": len(task.get("evidence") or []),
                **({"Article state": article["state"]} if article else {}),
            },
            "compression_identity": self._task_compression_identity(task),
            "specialized_review": bool(
                (task.get("repair_eligibility") or {}).get("adapter_id")
                or task.get("relationship_repair_proposal")
                or str(task.get("classification") or "").casefold() == "integrity"
            ),
        }
        item["batch_allowed_actions"] = tuple(
            ["resolve"] + (["resolve_verified"] if item["resolve_verified"] else [])
            + ([] if item["authoritative_status"] == "deferred" else ["defer"])
            + ["ignore"]
        )
        item["order_group"] = self._order_group(item, task)
        return item

    def _article_context(self, task: dict[str, Any]) -> dict[str, str]:
        if task.get("content_type") != "article":
            return {}
        identifier = str(task.get("content_identifier") or "")
        knowledge_root = self.root / "knowledge_base"
        if not all((knowledge_root / name).is_dir()
                   for name in ("drafts", "published", "archive", "deleted")):
            return {}
        repository = KnowledgeRepository(knowledge_root)
        article: dict[str, Any]
        state = "Published"
        try:
            article = repository.resolve_published_article(identifier)
        except (ArticleNotFoundError, KnowledgeRepositoryError):
            try:
                article = repository.get_draft(identifier)
                state = str(article.get("status") or "Draft").replace("_", " ").title()
            except (ArticleNotFoundError, KnowledgeRepositoryError):
                return {}
        review = article.get("review") or article.get("technical_review") or {}
        review_state = str(review.get("status") or article.get("review_status") or "").strip()
        if review_state:
            state = f"{state}; review {review_state.replace('_', ' ')}"
        preview = str(
            article.get("overview") or article.get("summary")
            or article.get("introduction") or ""
        ).strip()
        return {
            "title": str(article.get("title") or identifier),
            "state": state,
            "preview": preview[:360] + ("…" if len(preview) > 360 else ""),
        }

    def _fresh_corrected(self, task: dict[str, Any]) -> bool:
        verification = task.get("current_verification") or {}
        if str(task.get("confidence") or "").casefold() != "high":
            return False
        if verification.get("status") not in self.CORRECTED_VERIFICATION_STATUSES:
            return False
        if verification.get("rule") not in {None, "", task.get("curator_rule")}:
            return False
        stored = str(verification.get("affected_fingerprint") or "")
        if not stored or stored != str(task.get("last_verified_fingerprint") or ""):
            return False
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        if not workflow_id:
            return False
        if verification.get("workflow_id") not in {None, "", workflow_id}:
            return False
        if node_id and verification.get("node_id") not in {None, "", node_id}:
            return False
        lifecycle = CuratorWorkflowLifecycleService(self.root)
        if len(lifecycle.drafts(workflow_id)) > 1:
            return False
        target = lifecycle.resolve(workflow_id)
        if target is None:
            return False
        scope = str(verification.get("affected_fingerprint_scope") or "")
        if scope == "whole_workflow":
            current = target.fingerprint
        elif scope in {"", "affected_content", "workflow_node"}:
            current = CuratorTargetedVerificationService(self.root).current_fingerprint(task)
        else:
            return False
        return bool(current and current == stored)

    @staticmethod
    def _task_precedent(related: list[dict[str, Any]], calibration: dict[str, Any],
                        task: dict[str, Any]) -> str:
        parts = []
        if related:
            parts.append(f"{len(related)} related task{'s' if len(related) != 1 else ''}")
        disposition = str(task.get("review_disposition") or "").replace("_", " ").title()
        if disposition and disposition != "Not Reviewed":
            parts.append(f"prior disposition: {disposition}")
        similar = calibration.get("similar_task_count") or calibration.get("sample_count")
        if similar:
            parts.append(f"{similar} comparable reasoning reviews")
        return "; ".join(parts) or "No existing precedent summary is available."

    def _lesson_item(self, lesson: dict[str, Any]) -> dict[str, Any]:
        lesson_id = str(lesson.get("lesson_id") or "")
        item = {
            "key": f"growth_lesson:{lesson_id}", "item_type": "growth_lesson",
            "type_label": "Growth Lesson", "item_id": lesson_id,
            "title": str(lesson.get("display_title") or "Proposed Curator lesson"),
            "summary": str(lesson.get("recommended_future_behavior") or "Review this proposed operating lesson."),
            "priority": "", "risk": "Human-governed learning",
            "confidence": str(lesson.get("confidence") or ""),
            "affected_content": ", ".join(lesson.get("affected_domains") or []) or "Curator behavior",
            "affected_identity": str(lesson.get("raw_identity") or lesson.get("pattern_observed") or ""),
            "current_state": "Proposed", "current_detail": "No lesson has been adopted.",
            "recommendation": str(lesson.get("recommended_future_behavior") or "Review the proposed lesson."),
            "impact": f"Supported by {lesson.get('observations', 0)} observation(s).",
            "precedent": f"{len(lesson.get('supporting_evidence') or [])} supporting evidence item(s).",
            "inspect_url": "/curator/growth#lessonsTitle", "task_url": "", "return_to": "",
            "allowed_actions": ("approve", "reject"), "resolve_verified": False,
            "authoritative_status": str(lesson.get("status") or ""),
            "batch_allowed_actions": ("approve", "reject"),
            "specialized_review": False,
            "source_fingerprint": self._fingerprint(lesson),
            "technical": {"Lesson": lesson_id, "Source identity": str(lesson.get("raw_identity") or lesson.get("pattern_observed") or "")},
            "compression_identity": self._lesson_compression_identity(lesson),
        }
        item["order_group"] = self._order_group(item, lesson)
        return item

    def _proposal_item(self, proposal: dict[str, Any]) -> dict[str, Any]:
        proposal_id = str(proposal.get("proposal_id") or "")
        item = {
            "key": f"growth_capability:{proposal_id}", "item_type": "growth_capability",
            "type_label": "Capability Proposal", "item_id": proposal_id,
            "title": str(proposal.get("proposed_capability") or "Proposed Curator capability"),
            "summary": str(proposal.get("problem_addressed") or "Review this proposed capability."),
            "priority": "", "risk": ", ".join(proposal.get("risks") or []) or "Human-governed capability",
            "confidence": str(proposal.get("confidence") or ""),
            "affected_content": str(proposal.get("scope") or "Curator capability"),
            "affected_identity": proposal_id, "current_state": "Proposed",
            "current_detail": "The capability is not approved or active.",
            "recommendation": str(proposal.get("expected_benefit") or "Review the proposed capability."),
            "impact": str(proposal.get("expected_benefit") or "No impact summary supplied."),
            "precedent": f"{len(proposal.get('supporting_task_ids') or [])} supporting task(s).",
            "inspect_url": "/curator/growth#proposalsTitle", "task_url": "", "return_to": "",
            "allowed_actions": ("approve", "reject"), "resolve_verified": False,
            "authoritative_status": str(proposal.get("status") or ""),
            "batch_allowed_actions": (),
            "specialized_review": True,
            "source_fingerprint": self._fingerprint(proposal),
            "technical": {"Proposal": proposal_id, "Kind": "capability"},
            "compression_identity": self._proposal_compression_identity(proposal),
        }
        item["order_group"] = self._order_group(item, proposal)
        return item

    def _command_relationship_items(self) -> list[dict[str, Any]]:
        """Project persisted command-review gates without refreshing campaign state."""
        campaign_root = self.root / "knowledge_campaigns"
        planner = KnowledgeCoveragePlannerService(self.root, campaign_root)
        try:
            records = KnowledgeCampaignOrchestrationService.read_persisted(campaign_root)
        except KnowledgeCampaignOrchestrationError:
            return []

        projected = []
        for record in records:
            campaign_id = str(record.get("campaign_id") or "")
            orchestration_id = str(record.get("orchestration_id") or "")
            if not campaign_id or not orchestration_id or record.get("status") == "completed":
                continue
            try:
                campaign = planner.get(campaign_id)
            except KnowledgeCoveragePlannerError:
                continue
            if campaign.get("status") in {"completed", "archived"}:
                continue
            work_items = [
                item for item in campaign.get("work_items") or []
                if isinstance(item, dict) and item.get("work_item_id")
            ]
            for state in record.get("work_item_states") or []:
                if not self._is_pending_command_review(state):
                    continue
                work_id = str(state.get("work_item_id") or "")
                matches = [
                    item for item in work_items
                    if str(item.get("work_item_id") or "") == work_id
                ]
                if len(matches) != 1 or matches[0].get("work_type") != "command_reference":
                    continue
                item, _, _ = self._command_relationship_item(
                    record, campaign, state, matches[0]
                )
                if item:
                    projected.append(item)
        counts = Counter(item["key"] for item in projected)
        return [item for item in projected if counts[item["key"]] == 1]

    def command_relationship_review_status(
        self, campaign_id: str, work_item_id: str
    ) -> dict[str, Any]:
        """Explain whether one persisted campaign gate satisfies the strict projection."""
        campaign_root = self.root / "knowledge_campaigns"
        try:
            records = [
                record
                for record in KnowledgeCampaignOrchestrationService.read_persisted(campaign_root)
                if record.get("campaign_id") == campaign_id
            ]
            campaign = KnowledgeCoveragePlannerService(self.root, campaign_root).get(campaign_id)
        except (KnowledgeCampaignOrchestrationError, KnowledgeCoveragePlannerError):
            return {"projectable": False, "reason": "authoritative_campaign_unavailable"}
        if len(records) != 1:
            return {"projectable": False, "reason": "orchestration_identity_ambiguous"}
        states = [state for state in records[0].get("work_item_states") or [] if (
            isinstance(state, dict) and state.get("work_item_id") == work_item_id
        )]
        works = [work for work in campaign.get("work_items") or [] if (
            isinstance(work, dict) and work.get("work_item_id") == work_item_id
        )]
        if len(states) != 1 or len(works) != 1:
            return {"projectable": False, "reason": "work_identity_ambiguous"}
        if not self._is_pending_command_review(states[0]):
            return {"projectable": False, "reason": "command_human_gate_changed"}
        item, reason, prerequisite = self._command_relationship_item(
            records[0], campaign, states[0], works[0]
        )
        if item is not None and not item.get("decision_available"):
            return {
                "projectable": False,
                "reason": "missing_relationship_decision_handoff",
                "canonical_review_key": self.command_relationship_review_key(work_item_id),
                "missing_prerequisite": self.command_relationship_handoff(works[0], []),
            }
        return {
            "projectable": item is not None,
            "reason": reason,
            "canonical_review_key": self.command_relationship_review_key(work_item_id),
            "missing_prerequisite": prerequisite,
        }

    @classmethod
    def command_relationship_handoff(
        cls, work: dict[str, Any], absent_declarations: list[str]
    ) -> dict[str, Any]:
        return KnowledgeCoveragePlannerService.command_relationship_handoff(
            work, absent_declarations
        )

    @staticmethod
    def _is_pending_command_review(state: Any) -> bool:
        return bool(
            isinstance(state, dict)
            and state.get("action_authority") == "human_gate"
            and state.get("state") == "awaiting_human_review"
            and state.get("next_action") == "review_command_reference"
        )

    def _command_relationship_item(
        self,
        orchestration: dict[str, Any],
        campaign: dict[str, Any],
        state: dict[str, Any],
        work: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        work_id = str(work.get("work_item_id") or "")
        workflow_id = str(work.get("workflow_id") or "")
        node_id = str(work.get("node_id") or "")
        article_id = str(work.get("article_id") or "")
        command_id = str(work.get("command_identity") or "")
        if not all((work_id, workflow_id, node_id, article_id, command_id)):
            return None, "work_identity_incomplete", None

        lifecycle = CuratorWorkflowLifecycleService(self.root)
        if len(lifecycle.drafts(workflow_id)) > 1:
            return None, "workflow_identity_ambiguous", None
        target = lifecycle.resolve(workflow_id)
        node = (target.workflow.get("nodes") or {}).get(node_id) if target else None
        if (
            target is None
            or target.workflow_id != workflow_id
            or not isinstance(node, dict)
            or str(node.get("knowledge_article") or "") != article_id
        ):
            return None, "workflow_node_article_conflict", None

        command = self._command_record(command_id)
        article = self._published_article(article_id)
        if not command or not article:
            return None, "authoritative_relationship_record_missing", None
        if str(command.get("id") or "") != command_id:
            return None, "command_identity_conflict", None

        absent_declarations = []
        if "related_commands" in article:
            article_commands = article.get("related_commands")
            if not isinstance(article_commands, list):
                return None, "relationship_declaration_malformed", None
        else:
            article_commands = []
            absent_declarations.append("article.related_commands")
        if "related_articles" in command:
            command_articles = command.get("related_articles")
            if not isinstance(command_articles, list):
                return None, "relationship_declaration_malformed", None
        else:
            command_articles = []
            absent_declarations.append("command.related_articles")
        prerequisite = self.command_relationship_handoff(work, absent_declarations)
        handoff_valid = work.get("command_relationship_review_handoff") == prerequisite
        if absent_declarations:
            if work.get("command_relationship_review_handoff") != prerequisite:
                return None, "missing_relationship_declaration_handoff", prerequisite
        proposed_changes = []
        if command_id not in article_commands:
            proposed_changes.append(
                f"Add '{command_id}' to article '{article_id}' related_commands."
            )
        if article_id not in command_articles:
            proposed_changes.append(
                f"Add '{article_id}' to command '{command_id}' related_articles."
            )
        if not proposed_changes:
            return None, "relationship_already_complete", None

        key = self.command_relationship_review_key(work_id)
        if not key:
            return None, "review_item_identity_invalid", None
        return_to = "/review?" + urlencode({"item": key})
        command_url = f"/commands/{quote(command_id, safe='')}?" + urlencode({
            "return_to": return_to,
        })
        campaign_url = (
            f"/curator/growth/coverage-campaigns/{quote(str(campaign['campaign_id']), safe='')}"
            "/orchestration?" + urlencode({"return_to": return_to})
        )
        risk = command.get("risk") if isinstance(command.get("risk"), dict) else {}
        risk_level = str(risk.get("level") or "Unknown")
        changes_system = bool(risk.get("changes_system"))
        workflow_title = str(target.workflow.get("name") or workflow_id)
        node_title = str(
            node.get("question") or node.get("title") or node.get("instruction")
            or node.get("message") or node_id
        )
        evidence = [str(value) for value in work.get("evidence") or [] if str(value).strip()]
        relationship_evidence_fingerprint = self._fingerprint({
            "gap_identity": str(work.get("gap_identity") or ""),
            "workflow_fingerprint": target.fingerprint,
            "article": article,
            "command": command,
        })
        source = {
            "orchestration": orchestration,
            "campaign": campaign,
            "workflow_fingerprint": target.fingerprint,
            "article_fingerprint": self._fingerprint(article),
            "command_fingerprint": self._fingerprint(command),
            "article_declarations": article_commands,
            "command_declarations": command_articles,
            "proposed_changes": proposed_changes,
            "relationship_evidence_fingerprint": relationship_evidence_fingerprint,
        }
        item = {
            "key": key,
            "item_type": "command_relationship_review",
            "type_label": "Command Relationship",
            "item_id": work_id,
            "finding_id": str(work.get("gap_id") or state.get("gap_id") or ""),
            "title": f"Review {command_id} relationship",
            "relationship_summary": (
                f"Decide whether command '{command_id}' meaningfully supports article "
                f"'{article.get('title') or article_id}' and should be declared on both records."
            ),
            "summary": (
                "The linked article contains a structured command reference resolving "
                "to the existing risk-classified Command Library record, but the explicit "
                "workflow/article/command relationship is incomplete."
            ),
            "priority": str(work.get("priority") or state.get("priority") or ""),
            "risk": f"{risk_level} risk · Changes system: {'Yes' if changes_system else 'No'}",
            "confidence": str(work.get("confidence") or ""),
            "affected_content": f"{workflow_title} · {node_title}",
            "affected_identity": f"{workflow_id}:{node_id}:{article_id}:{command_id}",
            "current_state": "Awaiting human review",
            "current_detail": (
                f"Campaign {campaign['campaign_id']} is at the Command Library relationship "
                f"review gate. Article related commands: "
                f"{', '.join(article_commands) or 'none declared'}. "
                f"Command related articles: {', '.join(command_articles) or 'none declared'}."
            ),
            "recommendation": (
                "Confirm that the command meaningfully supports the linked article before "
                "recording the exact reciprocal declarations."
            ),
            "impact": (
                "An incomplete explicit relationship makes governed discovery and integrity "
                "checks disagree with the structured command evidence used by this workflow."
            ),
            "precedent": "This campaign gate requires an individual human decision.",
            "evidence": evidence,
            "proposed_changes": proposed_changes,
            "relationship_preview": ({
                "record": "Article",
                "field": "related_commands",
                "value": command_id,
                "change": "Already declared" if command_id in article_commands else "Add",
            }, {
                "record": "Command",
                "field": "related_articles",
                "value": article_id,
                "change": "Already declared" if article_id in command_articles else "Add",
            }),
            "inspect_url": command_url,
            "inspect_label": "Inspect command",
            "campaign_url": campaign_url,
            "task_url": "",
            "return_to": return_to,
            "allowed_actions": (("approve", "reject") if handoff_valid else ()),
            "decision_available": handoff_valid,
            "decision_unavailable_reason": (
                "This campaign predates the authoritative command-relationship decision "
                "handoff. Reconcile the campaign before recording a decision."
                if not handoff_valid else ""
            ),
            "resolve_verified": False,
            "authoritative_status": str(state.get("state") or ""),
            "batch_allowed_actions": (),
            "specialized_review": True,
            "source_fingerprint": self._fingerprint(source),
            "relationship_evidence_fingerprint": relationship_evidence_fingerprint,
            "technical": {
                "Campaign": str(campaign.get("campaign_id") or ""),
                "Orchestration": str(orchestration.get("orchestration_id") or ""),
                "Work item": work_id,
                "Gap": str(work.get("gap_id") or ""),
                "Workflow": workflow_id,
                "Node": node_id,
                "Article": article_id,
                "Command": command_id,
                "Workflow source": target.source_path,
            },
            "compression_identity": {
                "key": f"command-relationship:{work_id}",
                "basis": "Command relationship campaign items are individually governed.",
            },
            "order_group": 1,
        }
        return item, "", None

    def _published_article(self, article_id: str) -> dict[str, Any] | None:
        """Read one exact canonical published record without creating repository paths."""
        if not article_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in article_id):
            return None
        path = self.root / "knowledge_base" / "published" / f"{article_id}.json"
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        identity = str(article.get("canonical_id") or article.get("id") or "")
        if identity != article_id:
            return None
        matches = 0
        for candidate in path.parent.glob("*.json"):
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if str(value.get("canonical_id") or value.get("id") or "") == article_id:
                matches += 1
        return article if matches == 1 else None

    def _command_record(self, command_id: str) -> dict[str, Any] | None:
        if not command_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in command_id
        ):
            return None
        directory = self.root / "knowledge_base" / "commands"
        path = directory / f"{command_id}.json"
        try:
            command = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(command.get("id") or "") != command_id:
            return None
        matches = 0
        for candidate in directory.glob("*.json"):
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if str(value.get("id") or "") == command_id:
                matches += 1
        return command if matches == 1 else None

    def _apply_compression(
        self,
        items: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        lessons: list[dict[str, Any]],
        proposals: list[dict[str, Any]],
    ) -> None:
        """Attach advisory grouping context using one bounded in-memory index."""
        open_counts = Counter(item["compression_identity"]["key"] for item in items)
        precedents: dict[str, list[tuple[str, str]]] = defaultdict(list)

        for task in tasks:
            disposition = str(task.get("review_disposition") or "NOT_REVIEWED").upper()
            status = str(task.get("status") or "").casefold()
            decision = ""
            if disposition != "NOT_REVIEWED":
                decision = disposition.replace("_", " ").title()
            elif status in {"resolved", "ignored"}:
                decision = status.title()
            if decision:
                identity = self._task_compression_identity(task)
                precedents[identity["key"]].append(
                    (str(task.get("task_id") or ""), decision)
                )

        for lesson in lessons:
            status = str(lesson.get("status") or "").casefold()
            if status in {"approved", "rejected", "retired"}:
                identity = self._lesson_compression_identity(lesson)
                precedents[identity["key"]].append(
                    (str(lesson.get("lesson_id") or ""), status.title())
                )

        for proposal in proposals:
            status = str(proposal.get("status") or "").casefold()
            if proposal.get("kind") == "capability" and status in {
                "approved", "rejected", "retired",
            }:
                identity = self._proposal_compression_identity(proposal)
                precedents[identity["key"]].append(
                    (str(proposal.get("proposal_id") or ""), status.title())
                )

        for item in items:
            identity = item.pop("compression_identity")
            item["batch_group_key"] = identity["key"]
            distribution = Counter(
                decision for item_id, decision in precedents.get(identity["key"], [])
                if item_id != item["item_id"]
            )
            prior_count = sum(distribution.values())
            if len(distribution) > 1:
                classification = "Mixed precedent"
            elif prior_count >= 2:
                classification = "Routine pattern"
            else:
                classification = "Novel / insufficient precedent"
            item["compression"] = {
                "classification": classification,
                "similar_open_count": open_counts[identity["key"]],
                "prior_count": prior_count,
                "prior_dispositions": dict(sorted(distribution.items())),
                "prior_summary": self._precedent_summary(distribution),
                "basis": identity["basis"],
                "advisory": (
                    "Similar-item history is advisory. This item still requires its own "
                    "human decision."
                ),
            }

        exclusion_reasons: dict[str, Counter] = defaultdict(Counter)
        eligible_counts = Counter()
        for item in items:
            reason = self._batch_exclusion_reason(item)
            if item["item_type"] in {"curator_task", "growth_lesson"} and not reason:
                eligible_counts[item["batch_group_key"]] += 1
            elif reason:
                exclusion_reasons[item["batch_group_key"]][reason] += 1
        for item in items:
            count = eligible_counts[item["batch_group_key"]]
            item["batch_review_count"] = count
            item["batch_excluded_count"] = sum(
                exclusion_reasons[item["batch_group_key"]].values()
            )
            item["batch_exclusion_reasons"] = [
                {"reason": reason, "count": excluded_count}
                for reason, excluded_count
                in sorted(exclusion_reasons[item["batch_group_key"]].items())
            ]
            item["batch_review_available"] = bool(
                count >= 2
                and item["item_type"] in {"curator_task", "growth_lesson"}
                and not self._batch_exclusion_reason(item)
            )

    @staticmethod
    def _precedent_summary(distribution: Counter) -> str:
        if not distribution:
            return "No materially similar prior decisions are recorded."
        return "; ".join(
            f"{count} {label.casefold()}" for label, count in sorted(distribution.items())
        ) + "."

    @staticmethod
    def _task_compression_identity(task: dict[str, Any]) -> dict[str, str]:
        rule = str(task.get("curator_rule") or "").upper()
        finding_type = str(task.get("finding_type") or "").casefold()
        content_type = str(task.get("content_type") or "").casefold()
        safety = str(task.get("safety_level") or "").casefold()
        category = str(task.get("category") or task.get("domain") or "").casefold()
        rule_label = WorkflowReasoningAuditor.RULE_LABELS.get(
            rule, ReviewWorkspaceService._humanize_rule(rule)
        )
        if rule.startswith("CUR-WR-"):
            calibration = ReasoningCalibrationService()
            snapshot = calibration.current_snapshot(task)
            if snapshot.get("structural_evidence"):
                fingerprint = str(snapshot.get("structural_fingerprint") or "")
                return {
                    "key": f"curator:reasoning:{rule}:{fingerprint}",
                    "basis": (
                        f"{rule_label} findings with the same deterministic workflow "
                        "structure."
                    ),
                }
        qualifiers = "|".join((rule, finding_type, content_type, safety, category))
        basis_parts = [f"{rule_label} findings", f"affected {content_type or 'content'}"]
        if safety:
            basis_parts.append(f"safety level {safety}")
        if category:
            basis_parts.append(category.replace("_", " "))
        return {
            "key": f"curator:exact:{qualifiers}",
            "basis": " · ".join(basis_parts) + ".",
        }

    @staticmethod
    def _lesson_compression_identity(lesson: dict[str, Any]) -> dict[str, str]:
        raw = str(
            lesson.get("raw_identity") or lesson.get("pattern_observed") or ""
        ).strip()
        parts = raw.split(":")
        if len(parts) == 4 and parts[0].casefold() == "reasoning_calibration":
            rule = parts[1].upper()
            fingerprint = parts[2].upper()
            label = WorkflowReasoningAuditor.RULE_LABELS.get(
                rule, ReviewWorkspaceService._humanize_rule(rule)
            )
            return {
                "key": f"growth:lesson:reasoning:{rule}:{fingerprint}",
                "basis": (
                    f"Proposed lessons for the {label} rule family and the same "
                    "deterministic calibration pattern."
                ),
            }
        normalized = raw.casefold()
        return {
            "key": f"growth:lesson:exact:{normalized}",
            "basis": "Proposed lessons with the same authoritative source identity.",
        }

    @staticmethod
    def _proposal_compression_identity(proposal: dict[str, Any]) -> dict[str, str]:
        capability = ReviewWorkspaceService._normalize_identity(
            proposal.get("proposed_capability")
        )
        scope = ReviewWorkspaceService._normalize_identity(proposal.get("scope"))
        return {
            "key": f"growth:proposal:capability:{capability}:{scope}",
            "basis": (
                "Plain capability proposals with the same capability name and declared "
                "scope."
            ),
        }

    @staticmethod
    def _normalize_identity(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().casefold())

    @staticmethod
    def _humanize_rule(rule: str) -> str:
        value = re.sub(r"^CUR-", "", rule).replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", value).strip().title() or "Curator"

    @staticmethod
    def _order_group(item: dict[str, Any], source: dict[str, Any]) -> int:
        priority = str(item.get("priority") or "").casefold()
        risk = str(item.get("risk") or "").casefold()
        rule = str(source.get("curator_rule") or "").casefold()
        if priority in {"critical", "high"} or "safety" in risk or rule.startswith("cur-safe"):
            return 0
        confidence = str(item.get("confidence") or "").casefold()
        observations = int(source.get("times_observed") or source.get("observations") or
                           source.get("recurrence_count") or 1)
        if confidence in {"low", "medium", ""} or observations <= 1:
            return 1
        if str(item.get("current_state") or "").casefold() in ReviewWorkspaceService.CORRECTED_VERIFICATION_STATUSES:
            return 2
        return 3

    @staticmethod
    def _queue_reason(item: dict[str, Any]) -> str:
        descriptions = {
            0: "High-priority and safety-related items are reviewed first; stable identity breaks ties.",
            1: "Governed items with limited precedent or confidence follow urgent work; stable identity breaks ties.",
            2: "A current correction signal is ready for human closure confirmation; stable identity breaks ties.",
            3: "This is the next remaining item in stable identity order.",
        }
        return descriptions.get(
            item.get("order_group"),
            "This item follows the Review queue's deterministic order.",
        )

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
