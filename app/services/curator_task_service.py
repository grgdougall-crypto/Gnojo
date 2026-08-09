from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from curator.memory import CuratorMemoryError, CuratorMemoryStore

from app.services.workflow_draft_service import WorkflowDraftService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService


class CuratorTaskService:
    """Operate on persistent Knowledge Tasks without modifying trusted content."""

    OWNERS = ("Curator", "Researcher", "Workflow Designer", "Script Engineer", "QA Reviewer", "Human")
    PRIORITIES = ("Critical", "High", "Medium", "Low", "Info")
    STATUSES = ("open", "in_progress", "deferred", "ignored", "resolved")

    def __init__(self, repository_root: Path | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.store = CuratorMemoryStore(self.repository_root / "curation_memory")

    def get(self, task_id: str, *, session_id: str = "", return_to: str = "",
            category: str = "all") -> dict[str, Any]:
        state = self.store.load()
        task = state.get("tasks", {}).get(task_id)
        if not task:
            raise CuratorMemoryError(f"Knowledge Task '{task_id}' was not found.")
        value = deepcopy(task)
        value.setdefault("explanation", "The Curator observed this condition during a deterministic repository audit.")
        value.setdefault("curator_rule", value.get("finding_type", "curator_observation"))
        value.setdefault("future_automated_fix", False)
        value.setdefault("resolution_notes", "")
        for field in ("related_content", "related_workflows", "related_articles", "related_commands", "related_scripts", "evidence", "history", "resolution_history"):
            value.setdefault(field, [])
        value["navigation"] = self._navigation(value, session_id=session_id,
                                                return_to=return_to, category=category)
        value["related_tasks"] = self._related_tasks(value, state.get("tasks", {}))
        value["audit_history"] = self._audit_history(value, state.get("audits", []))
        value["guidance"] = self._guidance(value)
        value["repair_preview"] = self._repair_preview(value)
        value["original_evidence"] = self._original_evidence(value)
        value["current_content"] = self._current_content(value)
        value["live_related_knowledge"] = self._live_related_knowledge(value)
        value["current_verification"] = deepcopy(value.get("current_verification") or {})
        value["saved_work_note_available"] = bool(self._latest_work_note(task_id))
        from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
        value["affected_fingerprint"] = CuratorTargetedVerificationService(
            self.repository_root).current_fingerprint(value)
        return value

    def update(self, task_id: str, *, action: str, owner: str = "", priority: str = "", note: str = "",
               session_id: str = "", expected_fingerprint: str = "") -> dict[str, Any]:
        status_by_action = {
            "start": "in_progress", "defer": "deferred", "ignore": "ignored",
            "resolve": "resolved", "reopen": "open",
        }
        if action not in {*status_by_action, "assign", "priority", "note"}:
            raise CuratorMemoryError(f"Unsupported task action: {action}")
        if action == "resolve" and not note.strip():
            note = self._latest_work_note(task_id)
        if action == "resolve" and not note.strip():
            raise CuratorMemoryError(
                "Add a resolution note describing what changed and what you verified before marking this task resolved."
            )
        if action == "resolve" and expected_fingerprint:
            from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
            task = self.store.load().get("tasks", {}).get(task_id, {})
            current = CuratorTargetedVerificationService(self.repository_root).current_fingerprint(task)
            if current != expected_fingerprint:
                raise CuratorMemoryError(
                    "The affected workflow changed after this page was loaded. Verify the current repair before resolving it."
                )
        return self.store.update_task(
            task_id,
            status=status_by_action.get(action),
            owner=owner or None,
            priority=priority or None,
            note=note.strip(),
            event_name=action,
            metadata={"maintenance_session_id": session_id} if action == "resolve" and session_id else None,
        )

    def _latest_work_note(self, task_id: str) -> str:
        """Reuse an explicitly saved human work note for the immediately following resolution."""
        task = self.store.load().get("tasks", {}).get(task_id, {})
        for event in reversed(task.get("history", [])):
            if event.get("actor") == "Human" and event.get("event") == "note" and str(event.get("note") or "").strip():
                return str(event["note"]).strip()
        return ""

    @staticmethod
    def _original_evidence(task: dict[str, Any]) -> list[str]:
        for event in task.get("history", []):
            if event.get("event") == "observed" and event.get("evidence"):
                return deepcopy(event["evidence"])
        return deepcopy(task.get("evidence", []))

    def _current_content(self, task: dict[str, Any]) -> dict[str, Any] | None:
        if task.get("content_type") not in {"workflow", "workflow_node"}:
            return None
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        target = CuratorWorkflowLifecycleService(self.repository_root).resolve(workflow_id)
        if not target:
            return None
        workflow = target.workflow
        node = (workflow or {}).get("nodes", {}).get(node_id) if node_id else None
        if not isinstance(node, dict):
            return None
        return {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "title": node.get("title") or node.get("question") or node_id,
            "instruction": node.get("instruction") or node.get("message") or node.get("help_text") or "",
            "help_text": node.get("help_text") or "",
            "knowledge_article": node.get("knowledge_article") or "",
            "source": target.filename,
            "source_path": target.source_path,
            "lifecycle": target.lifecycle,
        }

    def _live_related_knowledge(self, task: dict[str, Any]) -> dict[str, Any]:
        if task.get("content_type") not in {"workflow", "workflow_node"}:
            return {"articles": [], "source_path": None, "lifecycle": None}
        workflow_id, _, node_id = str(task.get("content_identifier") or "").partition(":")
        relationship = CuratorWorkflowLifecycleService(self.repository_root).relationship(workflow_id, node_id)
        articles = []
        if relationship.get("knowledge_article"):
            articles.append({"id": relationship["knowledge_article"],
                             "title": relationship.get("article_title"),
                             "status": relationship.get("status")})
        return {"articles": articles, "source_path": relationship.get("source_path"),
                "lifecycle": relationship.get("lifecycle"), "status": relationship.get("status")}

    def grouped(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active = [item for item in tasks if item.get("status") not in {"resolved", "ignored", "superseded"}]
        resolved = [item for item in tasks if item.get("status") == "resolved"]
        definitions = (
            ("Critical Today", [item for item in active if item.get("priority") in {"Critical", "High"}]),
            ("Trending", [item for item in active if item.get("trend") in {"recurring", "returned"}]),
            ("Frequently Recurring", [item for item in active if int(item.get("times_observed", 0)) >= 2]),
            ("Recently Created", sorted(active, key=lambda item: item.get("first_seen", ""), reverse=True)),
            ("Editorial Opportunities", [item for item in active if item.get("classification") == "Opportunity"]),
            ("Recommendations", [item for item in active if item.get("classification") == "Recommendation"]),
            ("Recently Resolved", sorted(resolved, key=lambda item: item.get("last_seen", ""), reverse=True)),
        )
        seen: set[str] = set()
        groups = []
        for title, items in definitions:
            unique = []
            for item in items:
                task_id = item.get("task_id", "")
                if task_id in seen:
                    continue
                seen.add(task_id)
                unique.append(item)
            if unique:
                groups.append({"title": title, "tasks": unique})
        remaining = [item for item in tasks if item.get("task_id") not in seen]
        if remaining:
            groups.append({"title": "All Other Tasks", "tasks": remaining})
        return groups

    def status(self, state: dict[str, Any]) -> dict[str, Any]:
        tasks = list(state.get("tasks", {}).values())
        audits = state.get("audits", [])
        latest = audits[-1] if audits else {}
        return {
            "current_state": "Idle",
            "last_audit": latest.get("completed_at"),
            "audit_duration": latest.get("duration_seconds"),
            "curator_version": latest.get("auditor_version", "1.0.0"),
            "memory_size": len(tasks) + len(audits) + len(state.get("decisions", [])),
            "active_tasks": sum(item.get("status") in {"open", "in_progress", "deferred"} for item in tasks),
            "resolved_tasks": sum(item.get("status") == "resolved" for item in tasks),
            "debt_trend": latest.get("knowledge_debt", {}).get("trend", "baseline"),
        }

    def evolution(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for audit in state.get("audits", [])[-20:]:
            changes = audit.get("task_changes", {})
            events.append({
                "at": audit.get("completed_at"), "event": "Audit completed",
                "detail": f"{audit.get('summary', {}).get('findings', 0)} findings; {len(changes.get('created', []))} tasks created and {len(changes.get('resolved', []))} resolved.",
            })
        for decision in state.get("decisions", [])[-20:]:
            events.append({"at": decision.get("at"), "event": decision.get("event", "Task updated").replace("_", " ").title(), "detail": f"{decision.get('task_id')}: {decision.get('note') or 'Task state changed.'}"})
        return sorted(events, key=lambda item: item.get("at") or "", reverse=True)[:25]

    def _navigation(self, task: dict[str, Any], *, session_id: str = "",
                    return_to: str = "", category: str = "all") -> dict[str, str]:
        kind = task.get("content_type", "")
        identifier = task.get("content_identifier", "")
        task_id = task.get("task_id", "")
        task_query = {"curator_session": session_id, "return_to": return_to,
                      "category": category, "verify": "1"}
        task_query = {key: value for key, value in task_query.items() if value}
        task_return = f"/curator/tasks/{quote(task_id)}"
        if task_query:
            task_return += "?" + urlencode(task_query)
        return_path = quote(task_return, safe="")
        if kind in {"workflow", "workflow_node"}:
            workflow_id, _, node_id = identifier.partition(":")
            drafts = WorkflowDraftService().list_drafts()
            draft = next((item for item in drafts if item.get("workflow_id") == workflow_id and not item.get("is_damaged")), None)
            if draft:
                query = {"curator_task": task_id, "curator_session": session_id,
                         "curator_return": task_return, "category": category}
                if node_id:
                    query["node"] = node_id
                suffix = "?" + urlencode({key: value for key, value in query.items() if value})
                return {"label": "Open affected workflow", "url": f"/workflow-editor/{quote(draft['filename'])}{suffix}", "kind": kind}
            return {"label": "Open Workflow Studio", "url": f"/workflow-studio?workflow={quote(workflow_id)}&curator_task={quote(task_id)}", "kind": kind}
        if kind == "article":
            draft = self.repository_root / "knowledge_base" / "drafts" / f"{identifier}.json"
            base = f"/knowledge/drafts/{quote(identifier)}" if draft.exists() else f"/knowledge/published/{quote(identifier)}"
            return {"label": "Open affected article", "url": f"{base}?return_to={return_path}", "kind": kind}
        if kind == "command":
            return {"label": "Open affected command", "url": f"/commands/{quote(identifier)}?return_to={return_path}", "kind": kind}
        if kind == "script":
            return {"label": "Open affected script", "url": f"/scripts/{quote(identifier)}?return_to={return_path}", "kind": kind}
        return {"label": "Open related workspace", "url": "/content-studio", "kind": kind or "application"}

    @staticmethod
    def _related_tasks(task: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        identifiers = set(task.get("related_content", [])) | set(task.get("related_workflows", []))
        related = []
        for candidate in tasks.values():
            if candidate.get("task_id") == task.get("task_id"):
                continue
            candidate_ids = set(candidate.get("related_content", [])) | set(candidate.get("related_workflows", []))
            if identifiers & candidate_ids:
                related.append({key: candidate.get(key) for key in ("task_id", "title", "classification", "status")})
        return related[:20]

    @staticmethod
    def _audit_history(task: dict[str, Any], audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        run_ids = {item.get("run_id") for item in task.get("history", [])}
        return [audit for audit in reversed(audits) if audit.get("run_id") in run_ids]

    @staticmethod
    def _guidance(task: dict[str, Any]) -> dict[str, Any]:
        classification = task.get("classification", "Observation")
        human_required = classification in {"Risk", "Opportunity", "Recommendation"} or not task.get("future_automated_fix")
        return {
            "why": task.get("explanation") or "This task preserves a recurring Curator observation until it is reviewed.",
            "impact": f"It currently contributes {task.get('knowledge_debt_score', 0)} points to Knowledge Debt.",
            "next_step": task.get("recommended_action"),
            "human_required": human_required,
            "certainty": "Human judgment is required." if human_required else "The Curator has enough evidence to preview a deterministic repair.",
        }

    def _repair_preview(self, task: dict[str, Any]) -> dict[str, Any] | None:
        if not task.get("future_automated_fix"):
            return None
        if task.get("curator_rule") == "CUR-REL-ARTICLE-CANDIDATE":
            from app.services.curator_article_link_repair_service import CuratorArticleLinkRepairService
            return CuratorArticleLinkRepairService(self.repository_root).preview(task["task_id"])
        return {
            "available": False,
            "reason": "This rule is marked as a future deterministic repair, but no trusted repair adapter is registered yet. No content will be changed.",
            "before": task.get("evidence", []),
            "after": [],
        }
