from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Iterable


REASONING_PREFIX = "CUR-WR-"
REVIEW_DISPOSITIONS = ("NOT_REVIEWED", "USEFUL", "INTENTIONAL", "FALSE_POSITIVE")


class ReasoningCalibrationService:
    """Consolidate human reasoning reviews as advisory, non-enforcement evidence."""

    def snapshot(self, task: dict[str, Any], disposition: str, *, reviewed_at: str) -> dict[str, Any]:
        structure = self.structural_evidence(task)
        descriptor = self.structural_pattern(task, structure)
        return {
            "finding_identity": str(task.get("finding_id") or task.get("durable_identity") or ""),
            "rule": str(task.get("curator_rule") or ""),
            "workflow_id": self.workflow_id(task),
            "node_id": self.node_id(task),
            "structural_evidence": structure,
            "structural_pattern": descriptor,
            "structural_fingerprint": self.fingerprint(descriptor),
            "disposition": disposition,
            "reviewed_at": reviewed_at,
            "finding_status_at_review": str(task.get("status") or "open"),
        }

    def current_snapshot(self, task: dict[str, Any]) -> dict[str, Any]:
        disposition = str(task.get("review_disposition") or "NOT_REVIEWED")
        stored = task.get("reasoning_calibration")
        if isinstance(stored, dict) and stored.get("disposition") == disposition:
            return deepcopy(stored)
        reviewed_at = self._reviewed_at(task)
        return self.snapshot(task, disposition, reviewed_at=reviewed_at)

    @staticmethod
    def workflow_id(task: dict[str, Any]) -> str:
        related = task.get("related_workflows") or []
        if related:
            return str(related[0])
        if task.get("content_type") in {"workflow", "workflow_node"}:
            return str(task.get("content_identifier") or "").split(":", 1)[0]
        return ""

    @staticmethod
    def node_id(task: dict[str, Any]) -> str:
        identifier = str(task.get("content_identifier") or "")
        return identifier.split(":", 1)[1] if ":" in identifier else ""

    @staticmethod
    def structural_evidence(task: dict[str, Any]) -> dict[str, Any]:
        stored = task.get("reasoning_structure")
        if isinstance(stored, dict):
            return deepcopy(stored)
        for item in task.get("evidence") or []:
            text = str(item)
            if not text.startswith("Structural evidence:"):
                continue
            try:
                value = ast.literal_eval(text.split(":", 1)[1].strip())
            except (SyntaxError, ValueError):
                return {}
            return deepcopy(value) if isinstance(value, dict) else {}
        return {}

    def structural_pattern(self, task: dict[str, Any], structure: dict[str, Any]) -> dict[str, Any]:
        rule = str(task.get("curator_rule") or "")
        pattern: dict[str, Any] = {"rule": rule}
        if rule == "CUR-WR-EARLY-CONVERGENCE":
            distance = self._integer(structure.get("distance"))
            pattern.update({
                "branch_count": len(structure.get("branch_labels") or []),
                "destination_count": len(set(structure.get("destinations") or [])),
                "distance_band": "direct" if distance <= 1 else "short" if distance == 2 else "extended",
            })
        elif rule == "CUR-WR-SIGNAL-RETENTION":
            pattern.update({
                "signal_count": len(structure.get("signals") or []),
                "shared_destination_count": len(structure.get("shared_destinations") or []),
                "terminal_result_count": len(structure.get("terminal_results") or []),
            })
        elif rule == "CUR-WR-ACTION-VERIFICATION":
            pattern["destination_count"] = len(structure.get("destinations") or [])
        elif rule == "CUR-WR-TERMINAL-EVIDENCE":
            pattern.update({
                "requirement": str(structure.get("requirement") or "unspecified"),
                "missing_count": len(structure.get("missing") or []),
                "affected_path_count": len(structure.get("affected_paths") or []),
            })
        elif rule == "CUR-WR-PROGRESS":
            configured = self._integer(structure.get("configured_progress"))
            maximum = self._integer(structure.get("maximum_progress"))
            pattern["progress_relation"] = "exceeds" if configured > maximum else "within"
        else:
            pattern["shape"] = {
                key: self._shape(value) for key, value in sorted(structure.items())
            }
        return pattern

    @staticmethod
    def fingerprint(descriptor: dict[str, Any]) -> str:
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        return "RCP-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16].upper()

    def summary(self, tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
        reasoning = [task for task in tasks if str(task.get("curator_rule") or "").startswith(REASONING_PREFIX)]
        counts = Counter(str(task.get("review_disposition") or "NOT_REVIEWED") for task in reasoning)
        reviewed = [task for task in reasoning if str(task.get("review_disposition") or "NOT_REVIEWED") != "NOT_REVIEWED"]
        by_rule: dict[str, Counter] = defaultdict(Counter)
        by_workflow: dict[str, Counter] = defaultdict(Counter)
        by_pattern: dict[str, Counter] = defaultdict(Counter)
        pattern_rules: dict[str, str] = {}
        ungrouped = 0
        for task in reviewed:
            disposition = str(task.get("review_disposition"))
            snapshot = self.current_snapshot(task)
            rule = snapshot["rule"]
            workflow = snapshot["workflow_id"] or "Unspecified"
            pattern_id = snapshot["structural_fingerprint"]
            by_rule[rule][disposition] += 1
            by_workflow[workflow][disposition] += 1
            if snapshot["structural_evidence"]:
                by_pattern[pattern_id][disposition] += 1
                pattern_rules[pattern_id] = rule
            else:
                ungrouped += 1
        pattern_rows = []
        for pattern_id, distribution in sorted(by_pattern.items()):
            pattern_rows.append({
                "structural_fingerprint": pattern_id,
                "rule": pattern_rules[pattern_id],
                "reviewed": sum(distribution.values()),
                "dispositions": dict(distribution),
                "mixed": len(distribution) > 1,
            })
        result: dict[str, Any] = {key: counts.get(key, 0) for key in REVIEW_DISPOSITIONS}
        result.update({
            "total": len(reasoning), "reviewed": len(reviewed),
            "historical_resolved_reviewed": sum(task.get("status") == "resolved" for task in reviewed),
            "by_rule": self._breakdown(by_rule),
            "by_workflow": self._breakdown(by_workflow),
            "by_pattern": pattern_rows,
            "mixed_pattern_count": sum(row["mixed"] for row in pattern_rows),
            "ungrouped_reviewed": ungrouped,
            "advisory_only": True,
        })
        return result

    def context(self, task: dict[str, Any], tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
        if not str(task.get("curator_rule") or "").startswith(REASONING_PREFIX):
            return {}
        fingerprint = self.current_snapshot(task)["structural_fingerprint"]
        peers = []
        for candidate in tasks:
            if candidate.get("task_id") == task.get("task_id"):
                continue
            disposition = str(candidate.get("review_disposition") or "NOT_REVIEWED")
            if disposition == "NOT_REVIEWED" or not str(candidate.get("curator_rule") or "").startswith(REASONING_PREFIX):
                continue
            if self.current_snapshot(candidate)["structural_fingerprint"] == fingerprint:
                peers.append(candidate)
        distribution = Counter(str(peer.get("review_disposition")) for peer in peers)
        return {
            "structural_fingerprint": fingerprint,
            "prior_review_count": len(peers),
            "dispositions": dict(distribution),
            "mixed": len(distribution) > 1,
            "advisory": "No automatic decision is applied. Prior reviews are advisory only.",
        }

    def recurring_lessons(self, tasks: Iterable[dict[str, Any]], *, minimum: int = 2) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for task in tasks:
            disposition = str(task.get("review_disposition") or "NOT_REVIEWED")
            rule = str(task.get("curator_rule") or "")
            if not rule.startswith(REASONING_PREFIX) or disposition == "NOT_REVIEWED":
                continue
            snapshot = self.current_snapshot(task)
            if not snapshot["structural_evidence"]:
                continue
            groups[(rule, snapshot["structural_fingerprint"], disposition)].append(task)
        lessons = []
        for (rule, fingerprint, disposition), evidence in sorted(groups.items()):
            if len(evidence) < minimum:
                continue
            lessons.append({
                "pattern": f"reasoning_calibration:{rule}:{fingerprint}:{disposition}",
                "observation": (
                    f"{rule} findings with structural pattern {fingerprint} were reviewed as "
                    f"{disposition.replace('_', ' ').title()} in {len(evidence)} examples."
                ),
                "evidence_task_ids": sorted(str(task.get("task_id")) for task in evidence),
                "human_gate": True,
                "advisory_only": True,
            })
        return lessons

    @staticmethod
    def _breakdown(values: dict[str, Counter]) -> list[dict[str, Any]]:
        return [
            {"key": key, "reviewed": sum(counts.values()), "dispositions": dict(counts),
             "mixed": len(counts) > 1}
            for key, counts in sorted(values.items())
        ]

    @staticmethod
    def _shape(value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            return {"type": "collection", "count": len(value)}
        if isinstance(value, dict):
            return {"type": "mapping", "keys": sorted(value)}
        return type(value).__name__

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _reviewed_at(task: dict[str, Any]) -> str:
        for event in reversed(task.get("history") or []):
            if event.get("event") == "reasoning_review_disposition":
                return str(event.get("at") or "")
        return ""
