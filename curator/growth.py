from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .governance import CuratorGovernanceError, CuratorGovernancePolicy, PERMISSIONS
from .memory import CuratorMemoryError, CuratorMemoryStore
from .runtime_rules import (
    CuratorRuntimeRuleError, runtime_rule_fingerprint, validate_runtime_rule,
)


PROPOSAL_KINDS = {"capability", "audit_rule", "repair_adapter", "scheduled_routine", "permission_request"}
EVENT_TYPES = {"content_published", "workflow_changed", "source_changed", "validation_failed", "manual_request"}
PROPOSAL_LIFECYCLES = {
    "capability": {"proposed": {"approved", "rejected", "revision_requested"},
                   "revision_requested": {"proposed", "rejected"}, "approved": {"suspended", "retired"},
                   "suspended": {"approved", "retired"}, "rejected": set(), "retired": set()},
    "audit_rule": {"proposed": {"test_only", "rejected", "revision_requested"},
                   "revision_requested": {"proposed", "rejected"},
                   "test_only": {"human_approved", "rejected", "suspended"},
                   "human_approved": {"active", "suspended", "retired"},
                   "active": {"suspended", "retired"}, "suspended": {"test_only", "retired"},
                   "rejected": set(), "retired": set()},
    "repair_adapter": {"proposed": {"sandbox_tested", "rejected", "revision_requested"},
                       "revision_requested": {"proposed", "rejected"},
                       "sandbox_tested": {"reviewed", "rejected"},
                       "reviewed": {"approved", "rejected", "revision_requested"},
                       "approved": {"enabled", "suspended", "retired"},
                       "enabled": {"suspended", "retired"},
                       "suspended": {"approved", "retired"}, "rejected": set(), "retired": set()},
    "scheduled_routine": {"proposed": {"approved", "rejected", "revision_requested"},
                          "revision_requested": {"proposed", "rejected"},
                          "approved": {"suspended", "retired"}, "suspended": {"approved", "retired"},
                          "rejected": set(), "retired": set()},
    "permission_request": {"proposed": {"approved", "rejected", "revision_requested"},
                           "revision_requested": {"proposed", "rejected"},
                           "approved": {"retired"}, "rejected": set(), "retired": set()},
}
LESSON_STATUSES = {"proposed", "approved", "rejected", "retired"}
EVALUATION_OUTCOMES = {
    "confirmed_finding", "candidate_false_positive", "candidate_false_negative",
    "human_disagreement", "inconclusive",
}


class CuratorGrowthError(RuntimeError):
    pass


class CuratorGrowthService:
    """Supervised operational learning and growth proposals; never activates itself."""

    def __init__(self, store: CuratorMemoryStore):
        self.store = store

    def dashboard(self) -> dict[str, Any]:
        state = self.store.load()
        growth = state["growth"]
        proposals = sorted(growth["proposals"].values(), key=lambda item: item["created_at"], reverse=True)
        lessons = sorted(growth["lessons"].values(), key=lambda item: item["updated_at"], reverse=True)
        for proposal in proposals:
            proposal["available_statuses"] = sorted(
                PROPOSAL_LIFECYCLES[proposal["kind"]].get(proposal["status"], set())
            )
            evaluation = proposal.get("evaluation", {})
            observations = int(evaluation.get("total_observations", 0))
            false_positives = int(evaluation.get("false_positives", 0))
            proposal["needs_review"] = observations >= 5 and false_positives / max(observations, 1) > 0.2
            proposal["shadow_readiness"] = self._shadow_readiness(proposal)
        return {
            "policy": CuratorGovernancePolicy.snapshot(), "controls": deepcopy(state["controls"]),
            "proposals": proposals, "lessons": lessons,
            "evaluations": deepcopy(growth["evaluations"]),
            "counts": {
                "pending_proposals": sum(item["status"] in {"proposed", "test_only", "sandbox_tested", "reviewed", "revision_requested", "human_approved"} for item in proposals),
                "proposed_lessons": sum(item["status"] == "proposed" for item in lessons),
                "active_rules": sum(item["kind"] == "audit_rule" and item["status"] == "active" for item in proposals),
                "enabled_adapters": sum(item["kind"] == "repair_adapter" and item["status"] == "enabled" for item in proposals),
            },
        }

    def propose(self, kind: str, data: dict[str, Any], *, actor: str = "Curator") -> dict[str, Any]:
        if kind not in PROPOSAL_KINDS:
            raise CuratorGrowthError(f"Unsupported proposal kind: {kind}")
        state = self.store.load()
        CuratorGovernancePolicy.authorize("audit", "write_audit_output", state["controls"])
        self._validate_proposal(kind, data)
        identity = "|".join((kind, str(data.get("proposed_capability") or data.get("name")),
                             str(data.get("problem") or data.get("problem_addressed"))))
        proposal_id = "CGP-" + hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        existing = state["growth"]["proposals"].get(proposal_id)
        if existing:
            return deepcopy(existing)
        now = self._now()
        proposal = {
            "proposal_id": proposal_id, "kind": kind, "status": "proposed",
            "proposed_capability": str(data.get("proposed_capability") or data.get("name")),
            "problem_addressed": str(data.get("problem_addressed") or data.get("problem")),
            "supporting_task_ids": sorted(set(data.get("supporting_task_ids") or [])),
            "recurrence_count": int(data.get("recurrence_count") or 1),
            "expected_benefit": str(data.get("expected_benefit") or ""),
            "scope": str(data.get("scope") or ""), "required_tools": list(data.get("required_tools") or []),
            "required_permissions": list(data.get("required_permissions") or []),
            "risks": list(data.get("risks") or []), "test_plan": list(data.get("test_plan") or []),
            "rollback_plan": str(data.get("rollback_plan") or ""),
            "confidence": data.get("confidence", "medium"),
            "code_changes_required": bool(data.get("code_changes_required")),
            "created_at": now, "updated_at": now, "created_by": actor,
            "human_gate": True, "decision_history": [], "shadow_results": [],
            "evaluation": self._empty_evaluation(),
        }
        for field in ("rule_id", "active_rule_fingerprint", "proposed_behavior",
                      "supporting_evidence", "limitations", "observation_ids", "runtime_rule"):
            if field in data:
                proposal[field] = deepcopy(data[field])
        if kind == "repair_adapter":
            proposal.update({
                "eligibility_conditions": deepcopy(data["eligibility_conditions"]),
                "before_after": deepcopy(data["before_after"]),
                "affected_file_types": list(data["affected_file_types"]),
                "audit_logging": deepcopy(data["audit_logging"]),
            })
        state["growth"]["proposals"][proposal_id] = proposal
        self._event(state, actor, "growth_proposal_created", proposal_id, "Proposal created; no capability activated.")
        self.store.save(state)
        return deepcopy(proposal)

    def record_lesson(self, data: dict[str, Any], *, actor: str = "Curator") -> dict[str, Any]:
        required = ("pattern_observed", "supporting_evidence", "recommended_future_behavior")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise CuratorGrowthError(f"Lesson is missing: {', '.join(missing)}")
        state = self.store.load()
        CuratorGovernancePolicy.authorize("audit", "write_audit_output", state["controls"])
        identity = str(data["pattern_observed"]).strip().casefold()
        lesson_id = "CGL-" + hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        now = self._now()
        lesson = state["growth"]["lessons"].get(lesson_id)
        if lesson:
            lesson["observations"] = max(int(lesson.get("observations", 1)), int(data.get("observations") or 1))
            lesson["supporting_evidence"] = sorted(set(lesson["supporting_evidence"] + list(data["supporting_evidence"])))
            lesson["updated_at"] = now
        else:
            lesson = {
                "lesson_id": lesson_id, "pattern_observed": data["pattern_observed"],
                "supporting_evidence": list(data["supporting_evidence"]),
                "observations": int(data.get("observations") or 1), "confidence": data.get("confidence", "medium"),
                "affected_domains": list(data.get("affected_domains") or []),
                "human_decisions": list(data.get("human_decisions") or []),
                "recommended_future_behavior": data["recommended_future_behavior"],
                "status": "proposed", "created_at": now, "updated_at": now,
                "decision_history": [], "human_gate": True,
            }
            state["growth"]["lessons"][lesson_id] = lesson
        self.store.save(state)
        return deepcopy(lesson)

    def record_evaluation(self, data: dict[str, Any], *, actor: str = "Curator") -> dict[str, Any]:
        """Record immutable, idempotent evidence about one rule/content observation."""
        required = ("rule_id", "rule_fingerprint", "content_identifier", "content_fingerprint",
                    "expected_behavior", "actual_behavior", "outcome", "evidence")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise CuratorGrowthError(f"Evaluation is missing: {', '.join(missing)}")
        if data["outcome"] not in EVALUATION_OUTCOMES:
            raise CuratorGrowthError(f"Unsupported evaluation outcome: {data['outcome']}")
        state = self.store.load()
        CuratorGovernancePolicy.authorize("audit", "write_audit_output", state["controls"])
        identity = "|".join(str(data[key]) for key in (
            "rule_id", "rule_fingerprint", "content_identifier", "content_fingerprint", "outcome"
        ))
        evaluation_id = "CGEV-" + hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        existing = state["growth"]["evaluations"].get(evaluation_id)
        if existing:
            return deepcopy(existing)
        now = self._now()
        evaluation = {
            "evaluation_id": evaluation_id,
            "rule_id": str(data["rule_id"]),
            "rule_fingerprint": str(data["rule_fingerprint"]),
            "content_identifier": str(data["content_identifier"]),
            "content_fingerprint": str(data["content_fingerprint"]),
            "expected_behavior": str(data["expected_behavior"]),
            "actual_behavior": str(data["actual_behavior"]),
            "outcome": str(data["outcome"]),
            "evidence": deepcopy(data["evidence"]),
            "task_id": str(data.get("task_id") or ""),
            "maintenance_session_id": str(data.get("maintenance_session_id") or ""),
            "confidence": str(data.get("confidence") or "medium"),
            "human_review_required": True,
            "created_by": actor,
            "created_at": now,
        }
        state["growth"]["evaluations"][evaluation_id] = evaluation
        self._event(state, actor, "rule_evaluation_recorded", evaluation_id,
                    "Observation recorded without changing the rule or affected content.")
        self.store.save(state)
        return deepcopy(evaluation)

    def decide_proposal(self, proposal_id: str, target_status: str, *, reviewer: str, reason: str) -> dict[str, Any]:
        state = self.store.load()
        proposal = state["growth"]["proposals"].get(proposal_id)
        if not proposal:
            raise CuratorGrowthError(f"Capability Proposal '{proposal_id}' was not found.")
        reviewer, reason = self._require_human(reviewer, reason)
        allowed = PROPOSAL_LIFECYCLES[proposal["kind"]].get(proposal["status"], set())
        if target_status not in allowed:
            raise CuratorGrowthError(f"{proposal['kind']} cannot move from {proposal['status']} to {target_status}.")
        if proposal["kind"] == "audit_rule" and target_status == "human_approved":
            readiness = self._shadow_readiness(proposal)
            if not readiness["passed"]:
                raise CuratorGrowthError("An audit rule cannot be approved until its latest shadow test passes.")
        if proposal["kind"] == "audit_rule" and target_status == "active":
            readiness = self._shadow_readiness(proposal)
            if not readiness["passed"]:
                raise CuratorGrowthError("An audit rule cannot be activated until its latest shadow test passes.")
            try:
                manifest = validate_runtime_rule(
                    proposal.get("runtime_rule") or {}, expected_rule_id=proposal.get("rule_id", "")
                )
            except CuratorRuntimeRuleError as error:
                raise CuratorGrowthError(str(error)) from error
        event = {"at": self._now(), "actor": reviewer, "event": "human_growth_decision",
                 "from": proposal["status"], "to": target_status, "reason": reason}
        proposal["status"] = target_status
        proposal["updated_at"] = event["at"]
        proposal["decision_history"].append(event)
        if proposal["kind"] == "audit_rule" and target_status == "active":
            proposal["activated_runtime_rule"] = deepcopy(manifest)
            proposal["activation"] = {
                "actor": reviewer, "at": event["at"],
                "manifest_fingerprint": runtime_rule_fingerprint(manifest),
                "proposal_id": proposal_id,
            }
        self._event(state, reviewer, "human_growth_decision", proposal_id, reason, metadata=event)
        self.store.save(state)
        return deepcopy(proposal)

    def decide_lesson(self, lesson_id: str, target_status: str, *, reviewer: str, reason: str) -> dict[str, Any]:
        if target_status not in LESSON_STATUSES - {"proposed"}:
            raise CuratorGrowthError(f"Unsupported lesson decision: {target_status}")
        state = self.store.load()
        lesson = state["growth"]["lessons"].get(lesson_id)
        if not lesson:
            raise CuratorGrowthError(f"Lesson '{lesson_id}' was not found.")
        reviewer, reason = self._require_human(reviewer, reason)
        if lesson["status"] in {"rejected", "retired"}:
            raise CuratorGrowthError(f"Lesson cannot move from {lesson['status']}.")
        event = {"at": self._now(), "actor": reviewer, "event": "human_lesson_decision",
                 "from": lesson["status"], "to": target_status, "reason": reason}
        lesson["status"] = target_status
        lesson["updated_at"] = event["at"]
        lesson["decision_history"].append(event)
        self._event(state, reviewer, "human_lesson_decision", lesson_id, reason, metadata=event)
        self.store.save(state)
        return deepcopy(lesson)

    def record_shadow_result(self, proposal_id: str, result: dict[str, Any]) -> dict[str, Any]:
        state = self.store.load()
        proposal = state["growth"]["proposals"].get(proposal_id)
        if not proposal or proposal["kind"] != "audit_rule" or proposal["status"] != "test_only":
            raise CuratorGrowthError("Only a test-only audit rule may record shadow results.")
        matrix = {key: int((result.get("confusion_matrix") or {}).get(key) or 0)
                  for key in ("true_positive", "true_negative", "false_positive", "false_negative")}
        value = {"at": self._now(), "findings": int(result.get("findings") or 0),
                 "false_positives": int(result.get("false_positives") or 0),
                 "overlap": int(result.get("overlap") or 0),
                 "runtime_ms": float(result.get("runtime_ms") or 0),
                 "affected_content": list(result.get("affected_content") or []),
                 "confidence": result.get("confidence", "medium"),
                 "historical_comparison": str(result.get("historical_comparison") or ""),
                 "confusion_matrix": matrix,
                 "fixture_results": deepcopy(result.get("fixture_results") or []),
                 "uncertain": int(result.get("uncertain") or 0),
                 "passed": bool(result.get("passed")) and matrix["false_positive"] == 0
                           and matrix["false_negative"] == 0
                           and sum(matrix.values()) > 0}
        proposal["shadow_results"].append(value)
        proposal["evaluation"]["total_observations"] += value["findings"]
        proposal["evaluation"]["false_positives"] += value["false_positives"]
        proposal["updated_at"] = value["at"]
        self.store.save(state)
        return deepcopy(proposal)

    @staticmethod
    def _shadow_readiness(proposal: dict[str, Any]) -> dict[str, Any]:
        results = proposal.get("shadow_results") or []
        if not results:
            return {"passed": False, "reason": "No shadow result has been recorded."}
        latest = results[-1]
        if not latest.get("passed"):
            return {"passed": False, "reason": "The latest shadow result failed."}
        return {"passed": True, "reason": "The latest shadow result passed; separate human approval is still required."}

    def set_control(self, control: str, disabled: bool, *, reviewer: str, reason: str) -> dict[str, Any]:
        if control not in {
            "global_disabled",
            "scheduled_runs_disabled",
            "stage_b_scheduled_runs_disabled",
        }:
            raise CuratorGrowthError("Unsupported Curator control.")
        reviewer, reason = self._require_human(reviewer, reason)
        state = self.store.load()
        state["controls"][control] = bool(disabled)
        state["controls"][f"{control}_at"] = self._now()
        state["controls"][f"{control}_by"] = reviewer
        state["controls"][f"{control}_reason"] = reason
        self._event(state, reviewer, "curator_control_changed", control, reason,
                    metadata={"disabled": bool(disabled)})
        self.store.save(state)
        return deepcopy(state["controls"])

    def enqueue_event(self, event_type: str, content_identifier: str, *, actor: str,
                      metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue a bounded targeted-audit request; it never starts a broad sync."""
        if event_type not in EVENT_TYPES:
            raise CuratorGrowthError(f"Unsupported Curator event: {event_type}")
        if not content_identifier.strip():
            raise CuratorGrowthError("A content identifier is required for targeted verification.")
        state = self.store.load()
        CuratorGovernancePolicy.authorize("targeted_audit", "create_knowledge_tasks", state["controls"])
        now = self._now()
        event_id = "CGE-" + hashlib.sha256(
            f"{event_type}|{content_identifier}|{now}".encode()
        ).hexdigest()[:12].upper()
        event = {
            "event_id": event_id, "event_type": event_type,
            "content_identifier": content_identifier.strip(), "actor": actor.strip() or "system",
            "status": "queued", "requested_operation": "targeted_audit",
            "broad_sync": False, "created_at": now, "metadata": deepcopy(metadata or {}),
        }
        state["growth"]["event_queue"].append(event)
        self._event(state, event["actor"], "targeted_audit_queued", event_id,
                    f"Queued verification for {content_identifier}.")
        self.store.save(state)
        return deepcopy(event)

    @staticmethod
    def _validate_proposal(kind: str, data: dict[str, Any]) -> None:
        required = ("proposed_capability", "problem_addressed", "expected_benefit", "scope",
                    "risks", "test_plan", "rollback_plan")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise CuratorGrowthError(f"Proposal is missing: {', '.join(missing)}")
        invalid = sorted(set(data.get("required_permissions") or []) - PERMISSIONS)
        if invalid:
            raise CuratorGrowthError(f"Unknown requested permissions: {', '.join(invalid)}")
        if kind == "repair_adapter":
            adapter_required = ("eligibility_conditions", "before_after", "affected_file_types", "audit_logging")
            missing = [key for key in adapter_required if not data.get(key)]
            if missing:
                raise CuratorGrowthError(f"Repair adapter proposal is missing: {', '.join(missing)}")
        if kind == "audit_rule" and data.get("runtime_rule"):
            try:
                validate_runtime_rule(data["runtime_rule"], expected_rule_id=str(data.get("rule_id") or ""))
            except CuratorRuntimeRuleError as error:
                raise CuratorGrowthError(str(error)) from error

    @staticmethod
    def _empty_evaluation() -> dict[str, Any]:
        return {key: 0 for key in ("total_observations", "confirmed_findings", "false_positives",
            "accepted_recommendations", "rejected_recommendations", "successful_repairs",
            "failed_repairs", "reversions", "human_corrections") } | {"average_confidence": 0.0}

    @staticmethod
    def _require_human(reviewer: str, reason: str) -> tuple[str, str]:
        reviewer, reason = reviewer.strip(), reason.strip()
        if not reviewer or not reason:
            raise CuratorGrowthError("A human reviewer and decision reason are required.")
        normalized = reviewer.casefold().replace("_", " ").replace("-", " ")
        if normalized in {"curator", "system", "agent", "automation", "gnojo curator"}:
            raise CuratorGrowthError("Curator and automated identities cannot approve Curator growth.")
        return reviewer, reason

    @staticmethod
    def _event(state: dict[str, Any], actor: str, event: str, subject_id: str, reason: str,
               metadata: dict[str, Any] | None = None) -> None:
        value = {"at": CuratorGrowthService._now(), "actor": actor, "event": event,
                 "subject_id": subject_id, "reason": reason}
        if metadata:
            value["metadata"] = deepcopy(metadata)
        state.setdefault("decisions", []).append(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
