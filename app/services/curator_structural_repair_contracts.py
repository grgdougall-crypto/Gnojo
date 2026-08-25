from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from copy import deepcopy
from types import MappingProxyType
from typing import Any


class StructuralRepairContractError(ValueError):
    """A supervised structural repair description is incomplete or ambiguous."""


class ImmutableMapping(Mapping[str, Any]):
    """Copied, read-only mapping that exposes no mutable dict-subclass surface."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any] | None = None):
        object.__setattr__(self, "_data", MappingProxyType(dict(value or {})))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __deepcopy__(self, memo):
        return self

    def __repr__(self) -> str:
        return f"ImmutableMapping({dict(self._data)!r})"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return ImmutableMapping({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def to_plain_data(value: Any) -> Any:
    """Return detached JSON-shaped data at explicit serialization boundaries."""
    if isinstance(value, Mapping):
        return {key: to_plain_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_plain_data(item) for item in value]
    if isinstance(value, list):
        return [to_plain_data(item) for item in value]
    return deepcopy(value)


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_placeholder(item) for item in value)
    return isinstance(value, str) and value.startswith("$")


def _text(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise StructuralRepairContractError(f"{label} is required.")
    return normalized


@dataclass(frozen=True)
class RouteEdge:
    source: str
    route: str
    destination: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RouteEdge":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("Each route edge must be an object.")
        return cls(_text(value.get("source"), "Edge source"),
                   _text(value.get("route"), "Edge route"),
                   _text(value.get("destination"), "Edge destination"))


@dataclass(frozen=True)
class WorkflowNodeSpecification:
    node_id: str
    node_type: str
    content: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, expected_type: str) -> "WorkflowNodeSpecification":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("A workflow node specification must be an object.")
        node_id = _text(value.get("node_id"), "Node ID")
        content = value.get("content")
        if not isinstance(content, dict):
            raise StructuralRepairContractError(f"Node '{node_id}' content must be an object.")
        node_type = _text(content.get("type"), f"Node '{node_id}' type")
        if node_type != expected_type:
            raise StructuralRepairContractError(
                f"Node '{node_id}' must be an {expected_type} node."
            )
        required = "instruction" if expected_type == "instruction" else "question"
        _text(content.get(required), f"Node '{node_id}' {required}")
        if expected_type == "instruction":
            _text(content.get("title"), f"Node '{node_id}' title")
        return cls(node_id, node_type, _freeze(content))


@dataclass(frozen=True)
class OutcomeNodeSpecification:
    node_id: str
    node_type: str
    content: Mapping[str, Any]
    terminal_semantics: str
    required_evidence: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutcomeNodeSpecification":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("An outcome node specification must be an object.")
        node_id = _text(value.get("node_id"), "Outcome node ID")
        content = value.get("content")
        if not isinstance(content, dict):
            raise StructuralRepairContractError(f"Outcome node '{node_id}' content must be an object.")
        node_type = _text(content.get("type"), f"Outcome node '{node_id}' type")
        if node_type != "resolution":
            raise StructuralRepairContractError(
                f"Outcome node '{node_id}' must use the supported resolution terminal type."
            )
        _text(content.get("title"), f"Outcome node '{node_id}' title")
        _text(content.get("message"), f"Outcome node '{node_id}' resolution guidance")
        if any(key in content for key in ("next", "answers", "skip_to", "next_workflow")):
            raise StructuralRepairContractError(
                f"Outcome node '{node_id}' must be explicitly terminal."
            )
        if _contains_placeholder(content):
            raise StructuralRepairContractError(
                f"Outcome node '{node_id}' may not contain unresolved placeholders."
            )
        required = value.get("required_evidence")
        if not isinstance(required, list) or not required or not all(str(item).strip() for item in required):
            raise StructuralRepairContractError(
                f"Outcome node '{node_id}' requires explicit evidence metadata."
            )
        return cls(
            node_id, node_type, _freeze(content),
            _text(value.get("terminal_semantics"), f"Outcome node '{node_id}' terminal semantics"),
            tuple(sorted(str(item).strip() for item in required)),
        )


@dataclass(frozen=True)
class EvidenceProbeSpecification:
    specification_id: str
    version: int
    evidence_key: str
    approved: bool
    approved_by: str
    approved_at: str
    evidence_node: WorkflowNodeSpecification
    result_node: WorkflowNodeSpecification
    result_routes: tuple[tuple[str, str], ...]
    outcome_nodes: tuple[OutcomeNodeSpecification, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceProbeSpecification":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("Evidence probe specification must be an object.")
        version = value.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StructuralRepairContractError("Evidence probe version must be a positive integer.")
        approved = value.get("approved") is True
        approved_by = str(value.get("approved_by") or "").strip()
        approved_at = str(value.get("approved_at") or "").strip()
        if approved and (not approved_by or not approved_at):
            raise StructuralRepairContractError(
                "Approved evidence probes require reviewer and approval timestamp."
            )
        evidence_node = WorkflowNodeSpecification.from_dict(
            value.get("evidence_node"), expected_type="instruction"
        )
        result_node = WorkflowNodeSpecification.from_dict(
            value.get("result_node"), expected_type="question"
        )
        if evidence_node.node_id == result_node.node_id:
            raise StructuralRepairContractError("Evidence and result nodes require distinct IDs.")
        if str(evidence_node.content.get("next") or "").strip() != result_node.node_id:
            raise StructuralRepairContractError(
                "The evidence-gathering node must route directly to its distinct result decision."
            )
        answers = result_node.content.get("answers")
        routes = value.get("result_routes")
        if isinstance(routes, list):
            try:
                routes = dict(routes)
            except (TypeError, ValueError):
                routes = None
        if not isinstance(answers, Mapping) or not isinstance(routes, dict) or len(routes) < 2:
            raise StructuralRepairContractError(
                "A result decision requires at least two explicit answer routes."
            )
        normalized_routes = []
        for answer_id, destination in routes.items():
            answer_id = _text(answer_id, "Result answer ID")
            destination = _text(destination, f"Destination for result '{answer_id}'")
            answer = answers.get(answer_id)
            if not isinstance(answer, Mapping) or str(answer.get("next") or "").strip() != destination:
                raise StructuralRepairContractError(
                    f"Result route '{answer_id}' must exactly match the decision node answer."
                )
            _text(answer.get("label"), f"Label for result '{answer_id}'")
            normalized_routes.append((answer_id, destination))
        if set(answers) != {key for key, _ in normalized_routes}:
            raise StructuralRepairContractError("Every decision answer requires one explicit result route.")
        if len({destination for _, destination in normalized_routes}) < 2:
            raise StructuralRepairContractError(
                "The result decision requires at least two distinct destinations."
            )
        outcomes_raw = value.get("outcome_nodes", [])
        if not isinstance(outcomes_raw, list):
            raise StructuralRepairContractError("Outcome nodes must be an explicit list.")
        outcomes = tuple(OutcomeNodeSpecification.from_dict(item) for item in outcomes_raw)
        all_node_ids = [evidence_node.node_id, result_node.node_id, *(item.node_id for item in outcomes)]
        if len(set(all_node_ids)) != len(all_node_ids):
            raise StructuralRepairContractError("Specification-owned node IDs must be distinct.")
        outcome_ids = {item.node_id for item in outcomes}
        for _, destination in normalized_routes:
            if destination.startswith("$") and destination != "$preserved_terminal":
                # Legacy specifications may retain a reviewed placeholder, but an outcome-bearing
                # specification must be fully resolved before it can preview.
                if outcomes:
                    raise StructuralRepairContractError(
                        "Specifications with outcome nodes may not contain unresolved result destinations."
                    )
            if destination in outcome_ids:
                continue
        return cls(
            _text(value.get("specification_id"), "Evidence probe specification ID"),
            version,
            _text(value.get("evidence_key"), "Required evidence key"),
            approved,
            approved_by,
            approved_at,
            evidence_node,
            result_node,
            tuple(normalized_routes),
            outcomes,
        )


@dataclass(frozen=True)
class AffectedPath:
    nodes: tuple[str, ...]
    missing: tuple[str, ...]
    predecessor_edge: RouteEdge

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AffectedPath":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("Each affected path must be an object.")
        nodes = value.get("nodes")
        missing = value.get("missing")
        if not isinstance(nodes, list) or len(nodes) < 2 or not all(str(item).strip() for item in nodes):
            raise StructuralRepairContractError("Affected paths require at least two node IDs.")
        if not isinstance(missing, list) or not missing or not all(str(item).strip() for item in missing):
            raise StructuralRepairContractError("Affected paths require explicit missing evidence keys.")
        edge = RouteEdge.from_dict(value.get("predecessor_edge"))
        if edge.source != str(nodes[-2]) or edge.destination != str(nodes[-1]):
            raise StructuralRepairContractError("The predecessor edge must identify the path's final edge.")
        return cls(tuple(str(item) for item in nodes), tuple(sorted(str(item) for item in missing)), edge)


@dataclass(frozen=True)
class StructuralRepairPlan:
    plan_id: str
    workflow_id: str
    terminal_id: str
    required_evidence_key: str
    affected_paths: tuple[AffectedPath, ...]
    predecessor_edges: tuple[RouteEdge, ...]
    probe: EvidenceProbeSpecification
    proposed_outcome_nodes: tuple[OutcomeNodeSpecification, ...]
    preserved_terminal: str
    changed_existing_edges: tuple[RouteEdge, ...]
    new_edges: tuple[RouteEdge, ...]
    preserved_existing_nodes: tuple[str, ...]
    unaffected_routes: tuple[RouteEdge, ...]
    expected_post_repair_rule: str
    expected_post_repair_status: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StructuralRepairPlan":
        if not isinstance(value, dict):
            raise StructuralRepairContractError("Structural repair plan must be an object.")
        terminal = _text(value.get("terminal_id"), "Terminal ID")
        evidence_key = _text(value.get("required_evidence_key"), "Required evidence key")
        paths_raw = value.get("affected_paths")
        edges_raw = value.get("predecessor_edges")
        if not isinstance(paths_raw, list) or not paths_raw:
            raise StructuralRepairContractError("At least one affected path is required.")
        if not isinstance(edges_raw, list) or not edges_raw:
            raise StructuralRepairContractError("At least one predecessor edge is required.")
        paths = tuple(AffectedPath.from_dict(item) for item in paths_raw)
        edges = tuple(RouteEdge.from_dict(item) for item in edges_raw)
        if len(set(edges)) != len(edges):
            raise StructuralRepairContractError("Predecessor edges must be unique.")
        if {path.predecessor_edge for path in paths} != set(edges):
            raise StructuralRepairContractError(
                "Predecessor edges must exactly cover the affected paths."
            )
        if any(path.nodes[-1] != terminal for path in paths):
            raise StructuralRepairContractError("Every affected path must end at the preserved terminal.")
        if any(evidence_key not in path.missing for path in paths):
            raise StructuralRepairContractError(
                "Every affected path must identify the required evidence key as missing."
            )
        probe = EvidenceProbeSpecification.from_dict(value.get("probe"))
        if not probe.approved or probe.evidence_key != evidence_key:
            raise StructuralRepairContractError(
                "The plan requires an approved probe for its exact evidence key."
            )
        proposed_raw = value.get("proposed_outcome_nodes", [])
        if not isinstance(proposed_raw, list):
            raise StructuralRepairContractError("Proposed outcome nodes must be an explicit list.")
        proposed = tuple(OutcomeNodeSpecification.from_dict(item) for item in proposed_raw)
        if proposed != probe.outcome_nodes:
            raise StructuralRepairContractError(
                "Proposed outcome nodes must exactly match the approved specification."
            )
        preserved = _text(value.get("preserved_terminal"), "Preserved terminal")
        if preserved != terminal:
            raise StructuralRepairContractError("The preserved terminal must match the affected terminal.")
        route_destinations = {destination for _, destination in probe.result_routes}
        if sum(destination == preserved for _, destination in probe.result_routes) != 1:
            raise StructuralRepairContractError(
                "Exactly one explicit result route must preserve the affected terminal."
            )
        outcome_ids = {item.node_id for item in proposed}
        if any(destination.startswith("$") for destination in route_destinations):
            raise StructuralRepairContractError("Validated plans may not contain unresolved destinations.")
        changed_raw = value.get("changed_existing_edges")
        if not isinstance(changed_raw, list):
            raise StructuralRepairContractError("Changed existing edges must be an explicit list.")
        changed = tuple(RouteEdge.from_dict(item) for item in changed_raw)
        if changed != edges:
            raise StructuralRepairContractError(
                "Changed existing edges must exactly identify the recorded predecessor edges."
            )
        new_raw = value.get("new_edges")
        if not isinstance(new_raw, list) or not new_raw:
            raise StructuralRepairContractError("New structural edges must be explicit.")
        new_edges = tuple(RouteEdge.from_dict(item) for item in new_raw)
        proposed_node_ids = {
            probe.evidence_node.node_id, probe.result_node.node_id, *outcome_ids,
        }
        if any(edge.source not in proposed_node_ids for edge in new_edges):
            raise StructuralRepairContractError("New edges must originate from proposed nodes.")
        preserved_nodes_raw = value.get("preserved_existing_nodes")
        if (not isinstance(preserved_nodes_raw, list)
                or not preserved_nodes_raw
                or not all(str(item).strip() for item in preserved_nodes_raw)):
            raise StructuralRepairContractError("Preserved existing nodes must be explicit.")
        preserved_nodes = tuple(str(item) for item in preserved_nodes_raw)
        if terminal not in preserved_nodes:
            raise StructuralRepairContractError("The affected terminal must remain an existing node.")
        if proposed_node_ids & set(preserved_nodes):
            raise StructuralRepairContractError(
                "Proposed node IDs may not collide with preserved existing nodes."
            )
        if not route_destinations <= (set(preserved_nodes) | outcome_ids):
            raise StructuralRepairContractError(
                "Every result destination must resolve to a preserved or proposed node."
            )
        expected_new_edges = {
            RouteEdge(probe.evidence_node.node_id, "next", probe.result_node.node_id),
            *(RouteEdge(probe.result_node.node_id, answer, destination)
              for answer, destination in probe.result_routes),
        }
        if set(new_edges) != expected_new_edges or len(new_edges) != len(expected_new_edges):
            raise StructuralRepairContractError(
                "New edges must exactly represent the approved probe and result routes."
            )
        unaffected_raw = value.get("unaffected_routes")
        if not isinstance(unaffected_raw, list):
            raise StructuralRepairContractError("Unaffected routes must be an explicit list.")
        unaffected = tuple(RouteEdge.from_dict(item) for item in unaffected_raw)
        if set(unaffected) & set(edges):
            raise StructuralRepairContractError("Affected and unaffected routes cannot overlap.")
        expected = value.get("expected_post_repair")
        if not isinstance(expected, dict):
            expected = {
                "rule": value.get("expected_post_repair_rule"),
                "status": value.get("expected_post_repair_status"),
            }
        rule = _text(expected.get("rule"), "Expected post-repair rule")
        status = _text(expected.get("status"), "Expected post-repair status")
        if rule != "CUR-WR-TERMINAL-EVIDENCE" or status != "finding_absent":
            raise StructuralRepairContractError(
                "Structural evidence repairs must expect the terminal-evidence finding to be absent."
            )
        return cls(
            _text(value.get("plan_id"), "Repair plan ID"),
            _text(value.get("workflow_id"), "Workflow ID"),
            terminal,
            evidence_key,
            paths,
            edges,
            probe,
            proposed,
            preserved,
            changed,
            new_edges,
            preserved_nodes,
            unaffected,
            rule,
            status,
        )
