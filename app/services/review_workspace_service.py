from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
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
from curator.calibration import ReasoningCalibrationService
from curator.memory import CuratorMemoryError
from curator.workflow_reasoning import WorkflowReasoningAuditor


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
        self._apply_compression(projected, task_records, lessons, proposals)
        return sorted(projected, key=lambda item: (item["order_group"], item["key"]))

    def find(self, item_type: str, item_id: str) -> dict[str, Any] | None:
        return next((item for item in self.items()
                     if item["item_type"] == item_type and item["item_id"] == item_id), None)

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
        }
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
            "source_fingerprint": self._fingerprint(proposal),
            "technical": {"Proposal": proposal_id, "Kind": "capability"},
            "compression_identity": self._proposal_compression_identity(proposal),
        }
        item["order_group"] = self._order_group(item, proposal)
        return item

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
    def _fingerprint(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
