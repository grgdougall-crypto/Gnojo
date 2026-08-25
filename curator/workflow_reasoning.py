from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class ReasoningObservation:
    rule: str
    finding_type: str
    classification: str
    node_id: str
    title: str
    explanation: str
    evidence: tuple[str, ...]
    action: str
    severity: str = "low"
    confidence: str = "medium"
    structural: dict[str, Any] = field(default_factory=dict)


class WorkflowGraph:
    """Bounded, cycle-safe view of a Gnojo workflow graph."""

    def __init__(self, workflow: dict[str, Any], *, max_depth: int = 24):
        self.workflow = workflow
        self.nodes = workflow.get("nodes", {}) if isinstance(workflow.get("nodes"), dict) else {}
        self.start = str(workflow.get("start_node") or "")
        self.max_depth = max_depth

    def transitions(self, node_id: str) -> list[tuple[str, str]]:
        node = self.nodes.get(node_id)
        if not isinstance(node, dict):
            return []
        result: list[tuple[str, str]] = []
        direct = str(node.get("next") or "").strip()
        if direct:
            result.append(("next", direct))
        skip = str(node.get("skip_to") or "").strip()
        if skip:
            result.append(("conditions not matched", skip))
        answers = node.get("answers")
        if isinstance(answers, dict):
            for key, answer in answers.items():
                if not isinstance(answer, dict):
                    continue
                target = str(answer.get("next") or "").strip()
                if target:
                    result.append((str(answer.get("label") or key), target))
        return result

    def descendants(self, start: str, depth: int) -> dict[str, int]:
        found: dict[str, int] = {start: 0}
        queue = deque([(start, 0)])
        while queue:
            current, distance = queue.popleft()
            if distance >= min(depth, self.max_depth):
                continue
            for _, target in self.transitions(current):
                if target not in self.nodes:
                    continue
                next_distance = distance + 1
                if target not in found or next_distance < found[target]:
                    found[target] = next_distance
                    queue.append((target, next_distance))
        return found

    def shortest_path(self, start: str, target: str, depth: int) -> list[str]:
        """Return one bounded shortest path, including both endpoints."""
        queue = deque([(start, [start])])
        while queue:
            current, path = queue.popleft()
            if current == target:
                return path
            if len(path) - 1 >= min(depth, self.max_depth):
                continue
            for _, destination in self.transitions(current):
                if destination in self.nodes and destination not in path:
                    queue.append((destination, path + [destination]))
        return []

    def terminals_from(self, start: str) -> set[str]:
        terminals: set[str] = set()
        queue = deque([(start, 0, frozenset())])
        seen: set[tuple[str, int]] = set()
        while queue:
            current, depth, ancestry = queue.popleft()
            if depth > self.max_depth or current in ancestry or (current, depth) in seen:
                continue
            seen.add((current, depth))
            node = self.nodes.get(current, {})
            outgoing = self.transitions(current)
            if node.get("type") == "resolution" or not outgoing:
                terminals.add(current)
                continue
            next_ancestry = ancestry | {current}
            for _, target in outgoing:
                if target in self.nodes:
                    queue.append((target, depth + 1, next_ancestry))
        return terminals

    def paths_to(self, target: str) -> list[list[str]]:
        if not self.start or self.start not in self.nodes:
            return []
        paths: list[list[str]] = []
        stack = [(self.start, [self.start])]
        while stack and len(paths) < 128:
            current, path = stack.pop()
            if current == target:
                paths.append(path)
                continue
            if len(path) > self.max_depth:
                continue
            for _, destination in reversed(self.transitions(current)):
                if destination in self.nodes and destination not in path:
                    stack.append((destination, path + [destination]))
        return paths

    def max_user_visible_path(self) -> int:
        if self.start not in self.nodes:
            return 0
        maximum = 0
        stack = [(self.start, frozenset(), 0)]
        while stack:
            current, ancestry, count = stack.pop()
            if current in ancestry or len(ancestry) >= self.max_depth:
                continue
            node = self.nodes.get(current, {})
            visible = 1 if node.get("type") in {"question", "instruction", "resolution", "transition"} else 0
            next_count = count + visible
            maximum = max(maximum, next_count)
            next_ancestry = ancestry | {current}
            for _, target in self.transitions(current):
                if target in self.nodes:
                    stack.append((target, next_ancestry, next_count))
        return maximum


class WorkflowReasoningAuditor:
    RULE_LABELS = {
        "CUR-WR-EARLY-CONVERGENCE": "Early Branch Convergence",
        "CUR-WR-SIGNAL-RETENTION": "Strong Signal Not Preserved",
        "CUR-WR-ACTION-VERIFICATION": "Action Without Verification",
        "CUR-WR-TERMINAL-EVIDENCE": "Terminal Claim Exceeds Evidence",
        "CUR-WR-PROGRESS": "Progress Inconsistency",
    }
    CONVERGENCE_DEPTH = 3
    GENERIC_RESULTS = ("deeper", "additional", "recommended", "required", "investigation", "could not")
    DISTINCT_SIGNAL_WORDS = {
        "cpu", "memory", "disk", "storage", "network", "audio", "driver", "dns", "dhcp",
        "application", "startup", "security", "gateway", "adapter", "printer", "bluetooth",
    }
    ACTION_WORDS = (
        "restart", "install", "uninstall", "disable", "enable", "remove", "repair", "reset",
        "reconnect", "re-pair", "clear", "flush", "renew", "rollback", "roll back", "update",
        "cleanup", "free space", "close applications", "change", "configure",
    )
    VERIFY_WORDS = ("did ", "does ", "is ", "are ", "can ", "was ", "were ", "what ", "which ", "after")
    SUCCESS_LABELS = ("yes", "resolved", "works", "working", "restored", "successful", "success")
    FAILURE_LABELS = ("no", "not", "still", "failed", "failure", "unresolved")
    HANDLING_WORDS = ("turn on", "check ", "inspect ", "test ", "identify ", "verify ")
    EVIDENCE_REQUIREMENTS = {
        "dns_resolution": {
            "terminal_terms": ("dns resolution problem", "dns problem"),
            "required": {
                "gateway_reachability": ("gateway", "ping"),
                "external_ip_reachability": ("external ip", "public ip", "internet by ip", "upstream reachability"),
                "dns_resolution_test": ("nslookup", "dns resolution", "resolve dns"),
            },
        },
    }

    def analyze(self, workflow: dict[str, Any]) -> list[ReasoningObservation]:
        graph = WorkflowGraph(workflow)
        observations: list[ReasoningObservation] = []
        observations.extend(self._early_convergence(graph))
        observations.extend(self._strong_signal_loss(graph))
        observations.extend(self._actions_without_verification(graph))
        observations.extend(self._terminal_evidence(graph))
        progress = self._progress(graph)
        if progress:
            observations.append(progress)
        return observations

    def _early_convergence(self, graph: WorkflowGraph) -> list[ReasoningObservation]:
        found = []
        for node_id, node in graph.nodes.items():
            branches = graph.transitions(node_id) if node.get("type") == "question" else []
            material = [(label, target) for label, target in branches if not self._uncertain(label)]
            if len(material) < 2 or len({target for _, target in material}) < 2:
                continue
            descendants = [(label, target, graph.descendants(target, self.CONVERGENCE_DEPTH)) for label, target in material]
            candidates = set.intersection(*(set(item[2]) for item in descendants)) if descendants else set()
            candidates.discard(node_id)
            if not candidates:
                continue
            convergence = min(candidates, key=lambda item: (max(entry[2][item] for entry in descendants), item))
            distance = max(entry[2][convergence] for entry in descendants)
            if self._conditional_remediation_rejoin(graph, material, convergence, origin_node=node_id):
                continue
            if self._branch_handling_to_shared_verification(
                    graph, material, convergence, origin_node=node_id):
                continue
            if self._success_vs_continued_troubleshooting(
                    graph, material, convergence, origin_node=node_id):
                continue
            labels = [entry[0] for entry in descendants]
            found.append(ReasoningObservation(
                rule="CUR-WR-EARLY-CONVERGENCE", finding_type="workflow_reasoning_early_convergence",
                classification="opportunity", node_id=node_id,
                title="Distinct diagnostic branches converge quickly",
                explanation="Curator deterministically found different answer paths that rejoin within a small number of nodes. The convergence may be intentional, but a reviewer should confirm that branch-specific evidence is not discarded.",
                evidence=(f"Branches: {', '.join(labels)}", f"Convergence: {convergence}", f"Steps before convergence: {distance}"),
                action="Review whether each branch gathers and preserves enough distinct evidence before convergence.",
                structural={"branch_labels": labels, "destinations": [entry[1] for entry in descendants], "convergence_node": convergence, "distance": distance},
            ))
        return found

    def _conditional_remediation_rejoin(
        self, graph: WorkflowGraph, branches: list[tuple[str, str]], convergence: str,
        *, origin_node: str | None = None,
    ) -> dict[str, Any] | None:
        """Recognize a conservative perform-if-needed, otherwise-skip topology.

        Suppression requires exactly two material branches. One must enter the
        convergence node directly. The other must begin with an instruction
        already classified as a state-changing action and may pass only through
        outcome-verification questions before reaching the same node.
        """
        if len(branches) != 2:
            return None
        direct = [(label, target) for label, target in branches if target == convergence]
        if len(direct) != 1:
            return None
        action_branch = next((item for item in branches if item not in direct), None)
        if not action_branch:
            return None
        path = graph.shortest_path(action_branch[1], convergence, self.CONVERGENCE_DEPTH)
        if len(path) < 2 or path[-1] != convergence:
            return None
        remediation = graph.nodes.get(path[0], {})
        if remediation.get("type") != "instruction" or not self._is_action(remediation):
            return None
        intermediate = path[1:-1]
        if origin_node and origin_node in intermediate:
            return None
        if any(not self._is_verification(graph.nodes.get(node_id, {})) for node_id in intermediate):
            return None
        return {
            "direct_branch": direct[0][0],
            "action_branch": action_branch[0],
            "remediation_node": path[0],
            "verification_nodes": intermediate,
            "convergence_node": convergence,
            "path": path,
        }

    def _branch_handling_to_shared_verification(
        self, graph: WorkflowGraph, branches: list[tuple[str, str]], convergence: str,
        *, origin_node: str,
    ) -> dict[str, Any] | None:
        """Recognize two handled routes that prepare for one outcome check.

        Each route must contain its own procedural instruction before the same
        verification question.  A route through the originating question, an
        unbounded route, or an informational-only route remains reportable.
        """
        if len(branches) != 2 or not self._is_verification(graph.nodes.get(convergence, {})):
            return None
        paths: dict[str, list[str]] = {}
        handling_nodes: dict[str, list[str]] = {}
        for label, target in branches:
            path = graph.shortest_path(target, convergence, self.CONVERGENCE_DEPTH)
            if len(path) < 2 or path[-1] != convergence or origin_node in path[:-1]:
                return None
            handled = [node_id for node_id in path[:-1]
                       if self._is_branch_handling(graph.nodes.get(node_id, {}))]
            if not handled:
                return None
            paths[label] = path
            handling_nodes[label] = handled
        return {
            "pattern": "branch_specific_handling_to_shared_verification",
            "paths": paths,
            "handling_nodes": handling_nodes,
            "convergence_node": convergence,
        }

    def _success_vs_continued_troubleshooting(
        self, graph: WorkflowGraph, branches: list[tuple[str, str]], convergence: str,
        *, origin_node: str,
    ) -> dict[str, Any] | None:
        """Recognize success-now versus meaningful-work-before-same-resolution."""
        if len(branches) != 2 or not self._is_verification(graph.nodes.get(origin_node, {})):
            return None
        if graph.nodes.get(convergence, {}).get("type") != "resolution":
            return None
        successful = [(label, target) for label, target in branches
                      if target == convergence and self._label_matches(label, self.SUCCESS_LABELS)]
        if len(successful) != 1:
            return None
        continued = next((item for item in branches if item not in successful), None)
        if not continued or not self._label_matches(continued[0], self.FAILURE_LABELS):
            return None
        path = graph.shortest_path(continued[1], convergence, self.CONVERGENCE_DEPTH)
        if len(path) < 2 or path[-1] != convergence or origin_node in path[:-1]:
            return None
        handled = [node_id for node_id in path[:-1]
                   if self._is_branch_handling(graph.nodes.get(node_id, {}))]
        if not handled:
            return None
        return {
            "pattern": "success_vs_continued_troubleshooting",
            "success_branch": successful[0][0],
            "continued_branch": continued[0],
            "path": path,
            "handling_nodes": handled,
            "convergence_node": convergence,
        }

    def _strong_signal_loss(self, graph: WorkflowGraph) -> list[ReasoningObservation]:
        found = []
        for node_id, node in graph.nodes.items():
            if node.get("type") != "question":
                continue
            answers = [(label, target) for label, target in graph.transitions(node_id)
                       if any(word in label.casefold() for word in self.DISTINCT_SIGNAL_WORDS)]
            by_destination: dict[str, list[str]] = {}
            for label, target in answers:
                by_destination.setdefault(target, []).append(label)
            lost = {target: labels for target, labels in by_destination.items() if len(labels) > 1}
            if not lost:
                continue
            terminal_ids = set().union(*(graph.terminals_from(target) for target in lost))
            generic = [terminal for terminal in terminal_ids if self._generic_result(graph.nodes.get(terminal, {}))]
            if not generic:
                continue
            labels = sorted({label for values in lost.values() for label in values})
            found.append(ReasoningObservation(
                rule="CUR-WR-SIGNAL-RETENTION", finding_type="workflow_reasoning_signal_loss",
                classification="opportunity", node_id=node_id,
                title="Diagnostic specificity may be lost downstream",
                explanation="Distinct diagnostic signals share the same downstream route and can terminate in a generic result that does not structurally retain the selected signal.",
                evidence=(f"Signals: {', '.join(labels)}", f"Shared destinations: {', '.join(sorted(lost))}", f"Generic terminal results: {', '.join(sorted(generic))}"),
                action="Have a workflow reviewer decide whether the selected signal should remain visible in later checks or terminal guidance.",
                confidence="medium", structural={"signals": labels, "shared_destinations": sorted(lost), "terminal_results": sorted(generic)},
            ))
        return found

    def _actions_without_verification(self, graph: WorkflowGraph) -> list[ReasoningObservation]:
        found = []
        for node_id, node in graph.nodes.items():
            if node.get("type") != "instruction" or not self._is_action(node):
                continue
            targets = [target for _, target in graph.transitions(node_id)]
            if not targets:
                continue
            verified = any(self._is_verification(graph.nodes.get(target, {})) for target in targets)
            if verified:
                continue
            found.append(ReasoningObservation(
                rule="CUR-WR-ACTION-VERIFICATION", finding_type="workflow_reasoning_unverified_action",
                classification="risk", node_id=node_id,
                title="State-changing action may lack an outcome check",
                explanation="The instruction appears intended to change system state, but its immediate downstream node is not a question that verifies the result.",
                evidence=(f"Action node: {node_id}", f"Immediate destinations: {', '.join(targets)}"),
                action="Review whether an observable outcome check should follow this action.", severity="medium",
                structural={"action_node": node_id, "destinations": targets},
            ))
        return found

    def _terminal_evidence(self, graph: WorkflowGraph) -> list[ReasoningObservation]:
        found = []
        for terminal_id, terminal in graph.nodes.items():
            if terminal.get("type") != "resolution":
                continue
            terminal_text = self._node_text(terminal)
            for requirement_name, requirement in self.EVIDENCE_REQUIREMENTS.items():
                if not any(term in terminal_text for term in requirement["terminal_terms"]):
                    continue
                paths = graph.paths_to(terminal_id)
                if not paths:
                    continue
                missing_by_path = []
                for path in paths:
                    path_text = " ".join(self._node_text(graph.nodes.get(item, {})) for item in path)
                    missing = [name for name, markers in requirement["required"].items()
                               if not any(marker in path_text for marker in markers)]
                    if missing:
                        missing_by_path.append((path, missing))
                if not missing_by_path:
                    continue
                missing = sorted({item for _, values in missing_by_path for item in values})
                affected_paths = []
                predecessor_edges = []
                for path, path_missing in missing_by_path:
                    predecessor = self._path_predecessor_edge(graph, path, terminal_id)
                    affected_paths.append({
                        "nodes": list(path),
                        "missing": sorted(path_missing),
                        "predecessor_edge": predecessor,
                    })
                    if predecessor and predecessor not in predecessor_edges:
                        predecessor_edges.append(predecessor)
                found.append(ReasoningObservation(
                    rule="CUR-WR-TERMINAL-EVIDENCE", finding_type="workflow_reasoning_evidence_gap",
                    classification="risk", node_id=terminal_id,
                    title="Terminal diagnosis may exceed collected evidence",
                    explanation="A code-owned evidence requirement for this diagnosis is not satisfied on every route to the terminal result. This does not prove the diagnosis is wrong; it identifies a boundary for human review.",
                    evidence=(f"Requirement: {requirement_name}", f"Terminal: {terminal_id}", f"Missing evidence: {', '.join(missing)}", f"Affected paths: {len(missing_by_path)} of {len(paths)}"),
                    action="Review whether the workflow should collect the missing evidence or soften the terminal claim.",
                    severity="medium", confidence="high",
                    structural={
                        "requirement": requirement_name,
                        "terminal": terminal_id,
                        "missing": missing,
                        "affected_path_count": len(missing_by_path),
                        "affected_paths": affected_paths,
                        "predecessor_edges": predecessor_edges,
                    },
                ))
        return found

    @staticmethod
    def _path_predecessor_edge(graph: WorkflowGraph, path: list[str], terminal_id: str) -> dict[str, str] | None:
        if len(path) < 2 or path[-1] != terminal_id:
            return None
        source = path[-2]
        routes = [label for label, destination in graph.transitions(source) if destination == terminal_id]
        if len(routes) != 1:
            return None
        return {"source": source, "route": routes[0], "destination": terminal_id}

    def _progress(self, graph: WorkflowGraph) -> ReasoningObservation | None:
        configured = int(graph.workflow.get("estimated_steps") or 0)
        visible = graph.max_user_visible_path()
        if not configured or visible <= configured + 2:
            return None
        return ReasoningObservation(
            rule="CUR-WR-PROGRESS", finding_type="workflow_reasoning_progress_inconsistency",
            classification="opportunity", node_id="",
            title="Displayed progress may underrepresent user interactions",
            explanation="The longest bounded user-visible path materially exceeds the configured estimated step count, so several screens may share the same displayed progress position.",
            evidence=(f"Configured estimated steps: {configured}", f"Longest user-visible path: {visible}"),
            action="Review the progress model and decide whether user-visible interactions should be represented more accurately.",
            structural={"configured_steps": configured, "maximum_user_visible_nodes": visible},
        )

    @classmethod
    def _is_action(cls, node: dict[str, Any]) -> bool:
        text = cls._node_text(node)
        return any(word in text for word in cls.ACTION_WORDS)

    @classmethod
    def _is_verification(cls, node: dict[str, Any]) -> bool:
        if node.get("type") != "question":
            return False
        text = cls._node_text(node)
        return any(text.startswith(word) or word in text for word in cls.VERIFY_WORDS)

    @classmethod
    def _is_branch_handling(cls, node: dict[str, Any]) -> bool:
        if node.get("type") != "instruction":
            return False
        text = cls._node_text(node)
        return cls._is_action(node) or any(marker in text for marker in cls.HANDLING_WORDS)

    @staticmethod
    def _label_matches(label: str, markers: tuple[str, ...]) -> bool:
        value = label.casefold().strip()
        return any(value == marker or value.startswith(f"{marker} ") for marker in markers)

    @classmethod
    def _generic_result(cls, node: dict[str, Any]) -> bool:
        text = cls._node_text(node)
        return any(term in text for term in cls.GENERIC_RESULTS)

    @staticmethod
    def _uncertain(label: str) -> bool:
        value = label.casefold()
        return "not sure" in value or "unsure" in value or "unknown" in value

    @staticmethod
    def _node_text(node: dict[str, Any]) -> str:
        return " ".join(str(node.get(key) or "") for key in
                        ("title", "question", "instruction", "message", "help_text", "evidence_marker", "diagnostic_category")).casefold()
