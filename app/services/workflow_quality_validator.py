from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from app.services.workflow_progress_service import WorkflowProgressService


class WorkflowQualityValidator:
    """Deterministic graph and runtime-progress checks for decision-tree workflows."""

    TERMINAL_TYPES = {"resolution", "transition"}
    SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}

    def validate(
        self,
        workflow: dict[str, Any],
        available_workflow_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        workflow = workflow if isinstance(workflow, dict) else {}
        nodes = workflow.get("nodes")
        nodes = nodes if isinstance(nodes, dict) else {}
        start = workflow.get("start_node")
        known_workflows = (
            {str(value) for value in available_workflow_ids}
            if available_workflow_ids is not None
            else None
        )
        findings: list[dict[str, Any]] = []

        destinations = {
            node_id: self._destinations(node)
            for node_id, node in nodes.items()
            if isinstance(node, dict)
        }
        reachable = self._reachable(start, nodes, destinations)
        unreachable = sorted(set(nodes) - reachable)
        terminals = sorted(
            node_id
            for node_id in reachable
            if isinstance(nodes.get(node_id), dict)
            and nodes[node_id].get("type") in self.TERMINAL_TYPES
        )

        self._missing_destinations(nodes, destinations, findings)
        for node_id in unreachable:
            self._add(
                findings,
                "WARNING",
                "UNREACHABLE_NODE",
                node_id,
                "Node is not reachable from the workflow start node.",
                {"start_node": start},
                "Remove the node or connect it to an intentional branch.",
            )

        cycles = self._cycles(start, nodes, destinations)
        for cycle in cycles:
            self._add(
                findings,
                "ERROR",
                "CYCLE_DETECTED",
                cycle[0],
                "A reachable workflow route contains a cycle.",
                {"cycle": cycle},
                "Make the route bounded or end it at a terminal outcome.",
            )

        self._terminal_outgoing(nodes, reachable, findings)
        self._termination(nodes, destinations, reachable, terminals, findings)
        paths = self._terminating_paths(start, nodes, destinations)
        self._progress(workflow, nodes, paths, cycles, findings)
        self._uncertainty(nodes, destinations, reachable, findings)
        self._remediation_sequences(nodes, destinations, reachable, findings)
        self._handoffs(nodes, reachable, known_workflows, findings)

        lengths = [len(path) for path in paths]
        counts = {
            severity: sum(item["severity"] == severity for item in findings)
            for severity in ("ERROR", "WARNING", "INFO")
        }
        findings.sort(
            key=lambda item: (
                self.SEVERITY_ORDER[item["severity"]],
                item["rule"],
                item.get("node_id") or "",
            )
        )
        return {
            "workflow": {
                "workflow_id": workflow.get("workflow_id"),
                "name": workflow.get("name"),
                "progress_mode": workflow.get("progress_mode", "static"),
                "estimated_steps": workflow.get("estimated_steps"),
            },
            "overall_status": (
                "ERROR" if counts["ERROR"] else "WARNING" if counts["WARNING"] else "CLEAN"
            ),
            "checks": {
                "structure": self._check_status(findings, {
                    "MISSING_BRANCH_DESTINATION", "TERMINAL_OUTGOING_BRANCH"
                }),
                "reachability": self._check_status(findings, {"UNREACHABLE_NODE"}),
                "termination": self._check_status(findings, {
                    "CYCLE_DETECTED", "NONTERMINATING_PATH"
                }),
                "progress_integrity": self._check_status(findings, {
                    "PREMATURE_STATIC_PROGRESS", "STATIC_PATH_LENGTH_CONFLICT",
                    "BRANCH_PROGRESS_INTEGRITY", "UNKNOWN_PROGRESS_MODE"
                }),
                "uncertainty_handling": self._check_status(findings, {
                    "UNBOUNDED_UNCERTAINTY_BRANCH"
                }),
                "remediation_sequence": self._check_status(findings, {
                    "REPEATED_REMEDIATION_WITHOUT_EVIDENCE",
                    "ACTION_WITHOUT_VERIFICATION"
                }),
                "handoffs": self._check_status(findings, {"BROKEN_WORKFLOW_HANDOFF"}),
            },
            "findings": findings,
            "metrics": {
                "reachable_nodes": len(reachable),
                "unreachable_nodes": len(unreachable),
                "terminal_nodes": len(terminals),
                "terminating_paths": len(paths),
                "shortest_path": min(lengths) if lengths else None,
                "longest_path": max(lengths) if lengths else None,
                "cycles_detected": len(cycles),
                "findings_count": len(findings),
                "errors": counts["ERROR"],
                "warnings": counts["WARNING"],
                "info": counts["INFO"],
            },
        }

    @staticmethod
    def _destinations(node: dict[str, Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        if isinstance(node.get("skip_to"), str) and node["skip_to"]:
            result.append({"kind": "condition", "label": "skip_to", "target": node["skip_to"]})
        if node.get("type") == "question" and isinstance(node.get("answers"), dict):
            for answer_id, answer in node["answers"].items():
                target = answer.get("next") if isinstance(answer, dict) else answer
                if isinstance(target, str) and target:
                    result.append({"kind": "answer", "label": str(answer_id), "target": target})
        elif node.get("type") == "instruction" and isinstance(node.get("next"), str):
            result.append({"kind": "next", "label": "next", "target": node["next"]})
        return result

    @staticmethod
    def _reachable(start, nodes, destinations) -> set[str]:
        if start not in nodes:
            return set()
        found: set[str] = set()
        pending = [start]
        while pending:
            node_id = pending.pop()
            if node_id in found or node_id not in nodes:
                continue
            found.add(node_id)
            pending.extend(edge["target"] for edge in destinations.get(node_id, ()))
        return found

    def _missing_destinations(self, nodes, destinations, findings):
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            if node.get("type") == "question" and isinstance(node.get("answers"), dict):
                for answer_id, answer in node["answers"].items():
                    target = answer.get("next") if isinstance(answer, dict) else answer
                    if not isinstance(target, str) or not target:
                        self._add(
                            findings, "ERROR", "MISSING_BRANCH_DESTINATION", node_id,
                            f"Branch '{answer_id}' has no valid destination.",
                            {"kind": "answer", "label": str(answer_id), "target": target},
                            "Point the branch at an existing node.",
                        )
            if node.get("type") == "instruction":
                target = node.get("next")
                if not isinstance(target, str) or not target:
                    self._add(
                        findings, "ERROR", "MISSING_BRANCH_DESTINATION", node_id,
                        "Instruction has no valid next-node destination.",
                        {"kind": "next", "label": "next", "target": target},
                        "Point the instruction at an existing node.",
                    )
        for node_id, edges in destinations.items():
            for edge in edges:
                if edge["target"] not in nodes:
                    self._add(
                        findings, "ERROR", "MISSING_BRANCH_DESTINATION", node_id,
                        f"Branch '{edge['label']}' references missing node '{edge['target']}'.",
                        edge, "Point the branch at an existing node.",
                    )

    def _cycles(self, start, nodes, destinations) -> list[list[str]]:
        cycles: dict[tuple[str, ...], list[str]] = {}

        def visit(node_id, stack, positions):
            if node_id not in nodes:
                return
            if node_id in positions:
                cycle = stack[positions[node_id]:] + [node_id]
                body = cycle[:-1]
                rotations = [tuple(body[index:] + body[:index]) for index in range(len(body))]
                key = min(rotations) if rotations else (node_id,)
                cycles.setdefault(key, cycle)
                return
            positions[node_id] = len(stack)
            stack.append(node_id)
            for edge in destinations.get(node_id, ()):
                visit(edge["target"], stack, positions)
            stack.pop()
            positions.pop(node_id, None)

        if start in nodes:
            visit(start, [], {})
        return list(cycles.values())

    def _terminal_outgoing(self, nodes, reachable, findings):
        for node_id in reachable:
            node = nodes[node_id]
            if not isinstance(node, dict) or node.get("type") not in self.TERMINAL_TYPES:
                continue
            ordinary = [field for field in ("next", "answers") if node.get(field)]
            if ordinary:
                self._add(
                    findings, "ERROR", "TERMINAL_OUTGOING_BRANCH", node_id,
                    "Terminal node has an ordinary in-workflow outgoing branch.",
                    {"fields": ordinary},
                    "Remove the ordinary branch or use a nonterminal node type.",
                )

    def _termination(self, nodes, destinations, reachable, terminals, findings):
        reverse = {node_id: set() for node_id in reachable}
        for source in reachable:
            for edge in destinations.get(source, ()):
                if edge["target"] in reverse:
                    reverse[edge["target"]].add(source)
        can_terminate = set(terminals)
        pending = list(terminals)
        while pending:
            target = pending.pop()
            for source in reverse.get(target, ()):
                if source not in can_terminate:
                    can_terminate.add(source)
                    pending.append(source)
        for node_id in sorted(reachable - can_terminate):
            self._add(
                findings, "ERROR", "NONTERMINATING_PATH", node_id,
                "Reachable node cannot reach a resolution or workflow transition.",
                {"outgoing": [edge["target"] for edge in destinations.get(node_id, ())]},
                "Connect the route to a bounded terminal outcome.",
            )

    def _terminating_paths(self, start, nodes, destinations) -> list[list[str]]:
        paths: list[list[str]] = []

        def visit(node_id, path, seen):
            if node_id not in nodes or node_id in seen:
                return
            path = [*path, node_id]
            node = nodes[node_id]
            if isinstance(node, dict) and node.get("type") in self.TERMINAL_TYPES:
                paths.append(path)
                return
            for edge in destinations.get(node_id, ()):
                visit(edge["target"], path, seen | {node_id})

        visit(start, [], set())
        return paths

    def _progress(self, workflow, nodes, paths, cycles, findings):
        mode = workflow.get("progress_mode")
        estimate = workflow.get("estimated_steps")
        if mode not in (None, "", WorkflowProgressService.MODE):
            self._add(
                findings, "WARNING", "UNKNOWN_PROGRESS_MODE", None,
                f"Progress mode '{mode}' is not recognized.", {"progress_mode": mode},
                "Use static progress or the supported branch-aware mode.",
            )
            return
        if mode == WorkflowProgressService.MODE:
            if cycles:
                self._add(
                    findings, "ERROR", "BRANCH_PROGRESS_INTEGRITY", cycles[0][0],
                    "Branch-aware progress requires an acyclic reachable graph.",
                    {"cycles": len(cycles)}, "Remove cycles before using branch-aware progress.",
                )
            for path in paths:
                for step, node_id in enumerate(path, 1):
                    if nodes[node_id].get("type") in self.TERMINAL_TYPES:
                        continue
                    total = WorkflowProgressService.total(workflow, node_id, step)
                    if total <= step:
                        self._add(
                            findings, "ERROR", "BRANCH_PROGRESS_INTEGRITY", node_id,
                            "Branch-aware progress reaches completion at a nonterminal node.",
                            {"step": step, "total": total},
                            "Correct the graph or branch-aware progress calculation.",
                        )
            return
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 1:
            return
        premature = None
        for path in paths:
            for step, node_id in enumerate(path, 1):
                if step >= estimate and nodes[node_id].get("type") not in self.TERMINAL_TYPES:
                    premature = {"node_id": node_id, "step": step, "path_length": len(path)}
                    break
            if premature:
                break
        if premature:
            self._add(
                findings, "ERROR", "PREMATURE_STATIC_PROGRESS", premature["node_id"],
                "Static progress can display completion while meaningful interaction remains.",
                {**premature, "estimated_steps": estimate},
                "Review the estimate or opt into branch-aware progress through human review.",
            )
        lengths = sorted({len(path) for path in paths})
        if lengths and (len(lengths) > 1 or estimate not in lengths):
            self._add(
                findings, "WARNING", "STATIC_PATH_LENGTH_CONFLICT", None,
                "Static estimated steps do not represent every reachable terminating path.",
                {"estimated_steps": estimate, "path_lengths": lengths},
                "Review progress behavior across short and long branches.",
            )

    def _uncertainty(self, nodes, destinations, reachable, findings):
        for node_id in reachable:
            node = nodes[node_id]
            if not isinstance(node, dict) or node.get("type") != "question":
                continue
            answers = node.get("answers") if isinstance(node.get("answers"), dict) else {}
            for answer_id, answer in answers.items():
                label = answer.get("label", "") if isinstance(answer, dict) else ""
                normalized = f"{answer_id} {label}".lower().replace("’", "'")
                if not any(token in normalized for token in ("unsure", "not sure", "uncertain", "don't know")):
                    continue
                target = answer.get("next") if isinstance(answer, dict) else answer
                if isinstance(target, str) and self._can_reach(target, node_id, destinations):
                    self._add(
                        findings, "ERROR", "UNBOUNDED_UNCERTAINTY_BRANCH", node_id,
                        "Uncertainty branch can return to the same question and repeat indefinitely.",
                        {"answer": str(answer_id), "target": target},
                        "Bound repeated uncertainty with a distinct safe outcome.",
                    )

    def _remediation_sequences(self, nodes, destinations, reachable, findings):
        for node_id in reachable:
            node = nodes[node_id]
            if not isinstance(node, dict) or node.get("type") != "instruction":
                continue
            for edge in destinations.get(node_id, ()):
                target = nodes.get(edge["target"])
                if not isinstance(target, dict) or target.get("type") != "instruction":
                    continue
                evidence = {"first_action": node_id, "next_action": edge["target"]}
                self._add(
                    findings, "WARNING", "REPEATED_REMEDIATION_WITHOUT_EVIDENCE", node_id,
                    "One remediation instruction immediately leads to another instruction.",
                    evidence, "Confirm that new diagnostic evidence is gathered between actions.",
                )
                self._add(
                    findings, "WARNING", "ACTION_WITHOUT_VERIFICATION", node_id,
                    "An action is followed by additional remediation without a verification decision.",
                    evidence, "Add or confirm a verification/decision boundary where appropriate.",
                )

    def _handoffs(self, nodes, reachable, known_workflows, findings):
        if known_workflows is None:
            return
        for node_id in reachable:
            node = nodes[node_id]
            if not isinstance(node, dict) or node.get("type") != "transition":
                continue
            target = node.get("next_workflow")
            if isinstance(target, str) and target and target not in known_workflows:
                self._add(
                    findings, "ERROR", "BROKEN_WORKFLOW_HANDOFF", node_id,
                    f"Workflow handoff destination '{target}' is unavailable.",
                    {"next_workflow": target},
                    "Publish or correct the destination workflow before enabling the handoff.",
                )

    @staticmethod
    def _can_reach(start, target, destinations) -> bool:
        pending = deque([start])
        seen = set()
        while pending:
            node_id = pending.popleft()
            if node_id == target:
                return True
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(edge["target"] for edge in destinations.get(node_id, ()))
        return False

    @classmethod
    def _check_status(cls, findings, rules):
        relevant = [item for item in findings if item["rule"] in rules]
        if any(item["severity"] == "ERROR" for item in relevant):
            return "ERROR"
        if any(item["severity"] == "WARNING" for item in relevant):
            return "WARNING"
        return "PASS"

    @staticmethod
    def _add(findings, severity, rule, node_id, message, evidence, suggested_action):
        candidate = {
            "severity": severity,
            "rule": rule,
            "node_id": node_id,
            "message": message,
            "evidence": evidence,
            "suggested_action": suggested_action,
        }
        signature = (severity, rule, node_id, repr(evidence))
        if not any(
            (item["severity"], item["rule"], item["node_id"], repr(item["evidence"])) == signature
            for item in findings
        ):
            findings.append(candidate)
