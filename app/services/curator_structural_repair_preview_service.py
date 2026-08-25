from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from app.services.curator_structural_repair_contracts import (
    EvidenceProbeSpecification,
    StructuralRepairContractError,
    StructuralRepairPlan,
    to_plain_data,
)
from curator.workflow_reasoning import WorkflowGraph


class StructuralRepairPreviewError(ValueError):
    """Current evidence cannot produce one safe, deterministic structural preview."""


class CuratorStructuralRepairPreviewService:
    """Build an exact structural repair proposal without persisting any state."""

    RULE = "CUR-WR-TERMINAL-EVIDENCE"
    FINDING_TYPE = "workflow_reasoning_evidence_gap"
    PRESERVED_TERMINAL = "$preserved_terminal"

    def build(self, task: dict[str, Any], specification: EvidenceProbeSpecification,
              workflow: dict[str, Any]) -> StructuralRepairPlan:
        if task.get("curator_rule") != self.RULE or task.get("finding_type") != self.FINDING_TYPE:
            raise StructuralRepairPreviewError("The task is not a terminal-evidence finding.")
        if not specification.approved:
            raise StructuralRepairPreviewError("An approved evidence specification is required.")
        if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict):
            raise StructuralRepairPreviewError("The editable workflow is unavailable or malformed.")
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        task_workflow, _, task_node = str(task.get("content_identifier") or "").partition(":")
        if not workflow_id or task_workflow != workflow_id:
            raise StructuralRepairPreviewError("The task and editable workflow identities do not match.")
        structural = task.get("structured_evidence")
        if not isinstance(structural, dict):
            raise StructuralRepairPreviewError("Structured terminal-evidence details are unavailable.")
        terminal = str(structural.get("terminal") or "").strip()
        if not terminal or task_node != terminal or terminal not in workflow["nodes"]:
            raise StructuralRepairPreviewError("The affected terminal no longer matches the editable workflow.")
        missing = structural.get("missing")
        if not isinstance(missing, list) or missing != [specification.evidence_key]:
            raise StructuralRepairPreviewError(
                "One approved specification must exactly cover the task's single missing evidence key."
            )
        paths = structural.get("affected_paths")
        edges = structural.get("predecessor_edges")
        if not isinstance(paths, list) or not paths or not isinstance(edges, list) or not edges:
            raise StructuralRepairPreviewError("Affected paths and predecessor edges are required.")
        self._verify_current_evidence(workflow, paths, edges, terminal, specification.evidence_key)

        proposed_ids = self._proposed_ids(workflow, task, specification)
        action_id = proposed_ids[specification.evidence_node.node_id]
        result_id = proposed_ids[specification.result_node.node_id]
        probe = self._resolved_probe(specification, proposed_ids, terminal, workflow["nodes"])
        affected = {self._edge_key(edge) for edge in edges}
        unaffected = [edge for edge in self._workflow_edges(workflow) if self._edge_key(edge) not in affected]
        plan_seed = {
            "workflow_id": workflow_id,
            "task": self._task_identity(task),
            "specification": [specification.specification_id, specification.version],
            "predecessor_edges": edges,
            "proposed_ids": sorted(proposed_ids.items()),
        }
        plan_id = "SRP-" + self._hash(plan_seed)[:12].upper()
        plan_data = {
            "plan_id": plan_id,
            "workflow_id": workflow_id,
            "terminal_id": terminal,
            "required_evidence_key": specification.evidence_key,
            "affected_paths": deepcopy(paths),
            "predecessor_edges": deepcopy(edges),
            "probe": probe,
            "proposed_outcome_nodes": probe["outcome_nodes"],
            "preserved_terminal": terminal,
            "changed_existing_edges": deepcopy(edges),
            "new_edges": [
                {"source": action_id, "route": "next", "destination": result_id},
                *({"source": result_id, "route": answer, "destination": destination}
                  for answer, destination in probe["result_routes"].items()),
            ],
            "preserved_existing_nodes": sorted(workflow["nodes"]),
            "unaffected_routes": unaffected,
            "expected_post_repair": {"rule": self.RULE, "status": "finding_absent"},
        }
        try:
            return StructuralRepairPlan.from_dict(plan_data)
        except StructuralRepairContractError as error:
            raise StructuralRepairPreviewError(str(error)) from error

    def preview(self, task: dict[str, Any], specification: EvidenceProbeSpecification,
                workflow: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = self.build(task, specification, workflow)
        except StructuralRepairPreviewError as error:
            return {"available": False, "reason": str(error), "read_only": True}
        plan_data = to_plain_data(asdict(plan))
        before_edges = [asdict(edge) for edge in plan.predecessor_edges]
        proposed_edges = [
            {"before": edge, "after": {**edge, "destination": plan.probe.evidence_node.node_id}}
            for edge in before_edges
        ]
        result_routes = [
            {"answer": answer, "destination": destination,
             "preserves_terminal": destination == plan.preserved_terminal}
            for answer, destination in plan.probe.result_routes
        ]
        action_and_decision = [
            {"node_id": plan.probe.evidence_node.node_id,
             "content": to_plain_data(plan.probe.evidence_node.content)},
            {"node_id": plan.probe.result_node.node_id,
             "content": to_plain_data(plan.probe.result_node.content)},
        ]
        outcome_nodes = [
            {"node_id": item.node_id, "content": to_plain_data(item.content),
             "terminal_semantics": item.terminal_semantics,
             "required_evidence": list(item.required_evidence)}
            for item in plan.proposed_outcome_nodes
        ]
        fingerprint_payload = {
            "task": self._task_identity(task),
            "structured_evidence": task.get("structured_evidence"),
            "workflow": workflow,
            "affected_predecessor_edges": before_edges,
            "specification": self._specification_payload(specification),
            "validated_plan": plan_data,
        }
        return {
            "available": True,
            "read_only": True,
            "plan": plan_data,
            "specification": self._specification_payload(specification),
            "preview_token": self._hash(fingerprint_payload),
            "before": {
                "predecessor_edges": before_edges,
                "current_destinations": sorted({edge["destination"] for edge in before_edges}),
                "terminal": {
                    "node_id": plan.terminal_id,
                    "node": deepcopy(workflow["nodes"][plan.terminal_id]),
                },
            },
            "proposed": {
                "inserted_nodes": action_and_decision + outcome_nodes,
                "evidence_action_node": action_and_decision[0],
                "result_decision_node": action_and_decision[1],
                "outcome_nodes": outcome_nodes,
                "changed_predecessor_edges": proposed_edges,
                "changed_existing_edges": [asdict(edge) for edge in plan.changed_existing_edges],
                "new_edges": [asdict(edge) for edge in plan.new_edges],
                "result_routes": result_routes,
                "preserved_terminal_route": next(
                    route for route in result_routes if route["preserves_terminal"]
                ),
                "unaffected_routes": [asdict(edge) for edge in plan.unaffected_routes],
                "preserved_existing_nodes": list(plan.preserved_existing_nodes),
            },
        }

    def simulate(self, workflow: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
        """Apply an already validated preview to an in-memory copy only."""
        if not preview.get("available") or not isinstance(preview.get("proposed"), dict):
            raise StructuralRepairPreviewError("A validated structural preview is required.")
        simulated = deepcopy(workflow)
        proposed = preview["proposed"]
        for change in proposed.get("changed_predecessor_edges", []):
            before, after = change.get("before"), change.get("after")
            self._replace_edge(simulated, before, after)
        for node in proposed.get("inserted_nodes", []):
            node_id = str(node.get("node_id") or "")
            if not node_id or node_id in simulated["nodes"]:
                raise StructuralRepairPreviewError("A simulated inserted node collides with current topology.")
            simulated["nodes"][node_id] = deepcopy(node["content"])
        return simulated

    def _verify_current_evidence(self, workflow: dict[str, Any], paths: list[dict[str, Any]],
                                 edges: list[dict[str, Any]], terminal: str,
                                 evidence_key: str) -> None:
        current_edges = self._workflow_edges(workflow)
        current_keys = [self._edge_key(edge) for edge in current_edges]
        requested_keys = [self._edge_key(edge) for edge in edges if isinstance(edge, dict)]
        if len(requested_keys) != len(edges) or len(set(requested_keys)) != len(requested_keys):
            raise StructuralRepairPreviewError("Predecessor-edge evidence is incomplete or ambiguous.")
        for key in requested_keys:
            if current_keys.count(key) != 1:
                raise StructuralRepairPreviewError(
                    "A recorded predecessor edge no longer resolves exactly once in the editable workflow."
                )
        path_edges = []
        for path in paths:
            if not isinstance(path, dict) or evidence_key not in (path.get("missing") or []):
                raise StructuralRepairPreviewError("Affected-path evidence does not match the specification.")
            nodes = path.get("nodes")
            if not isinstance(nodes, list) or len(nodes) < 2 or nodes[-1] != terminal:
                raise StructuralRepairPreviewError("An affected path no longer identifies the terminal.")
            for source, destination in zip(nodes, nodes[1:]):
                if not any(edge["source"] == source and edge["destination"] == destination
                           for edge in current_edges):
                    raise StructuralRepairPreviewError(
                        "An affected path no longer exists in the editable workflow."
                    )
            predecessor = path.get("predecessor_edge")
            if self._edge_key(predecessor) not in requested_keys:
                raise StructuralRepairPreviewError(
                    "Affected paths and predecessor-edge evidence no longer agree."
                )
            path_edges.append(self._edge_key(predecessor))
        if set(path_edges) != set(requested_keys):
            raise StructuralRepairPreviewError(
                "Predecessor edges do not exactly cover the deficient paths."
            )

    def _resolved_probe(self, specification: EvidenceProbeSpecification,
                        proposed_ids: dict[str, str], terminal: str,
                        nodes: dict[str, Any]) -> dict[str, Any]:
        action_id = proposed_ids[specification.evidence_node.node_id]
        result_id = proposed_ids[specification.result_node.node_id]
        action = to_plain_data(specification.evidence_node.content)
        action["next"] = result_id
        decision = to_plain_data(specification.result_node.content)
        outcomes = []
        outcome_ids = set()
        for outcome in specification.outcome_nodes:
            resolved_id = proposed_ids[outcome.node_id]
            outcome_ids.add(resolved_id)
            outcomes.append({
                "node_id": resolved_id,
                "content": to_plain_data(outcome.content),
                "terminal_semantics": outcome.terminal_semantics,
                "required_evidence": list(outcome.required_evidence),
            })
        routes = {}
        for answer, destination in specification.result_routes:
            if destination == self.PRESERVED_TERMINAL:
                resolved = terminal
            else:
                resolved = proposed_ids.get(destination, destination)
            if resolved not in nodes and resolved not in outcome_ids:
                if destination.startswith("$reviewed_"):
                    raise StructuralRepairPreviewError(
                        f"Evidence result route '{answer}' requires an additional reviewed routing "
                        f"decision for '{destination}'."
                    )
                raise StructuralRepairPreviewError(
                    f"Evidence result route '{answer}' targets an unknown workflow node."
                )
            decision["answers"][answer]["next"] = resolved
            routes[answer] = resolved
        return {
            "specification_id": specification.specification_id,
            "version": specification.version,
            "evidence_key": specification.evidence_key,
            "approved": specification.approved,
            "approved_by": specification.approved_by,
            "approved_at": specification.approved_at,
            "evidence_node": {"node_id": action_id, "content": action},
            "result_node": {"node_id": result_id, "content": decision},
            "result_routes": routes,
            "outcome_nodes": outcomes,
        }

    def _proposed_ids(self, workflow: dict[str, Any], task: dict[str, Any],
                      specification: EvidenceProbeSpecification) -> dict[str, str]:
        used = set(workflow["nodes"])
        seed = {
            "workflow_id": workflow.get("workflow_id"),
            "task": self._task_identity(task),
            "specification": [specification.specification_id, specification.version],
        }
        proposed = {}
        definitions = [
            (specification.evidence_node.node_id, "evidence"),
            (specification.result_node.node_id, "result"),
            *((item.node_id, "outcome") for item in specification.outcome_nodes),
        ]
        for base, role in definitions:
            resolved = self._collision_safe_id(base, role, seed, used)
            proposed[base] = resolved
            used.add(resolved)
        return proposed

    @staticmethod
    def _replace_edge(workflow: dict[str, Any], before: dict[str, Any],
                      after: dict[str, Any]) -> None:
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise StructuralRepairPreviewError("A simulated edge change is malformed.")
        source = before.get("source")
        node = workflow.get("nodes", {}).get(source)
        if not isinstance(node, dict):
            raise StructuralRepairPreviewError("A simulated edge source no longer exists.")
        matches = []
        if before.get("route") == "next" and node.get("next") == before.get("destination"):
            matches.append((node, "next"))
        answers = node.get("answers")
        if isinstance(answers, dict):
            for answer_id, answer in answers.items():
                if (isinstance(answer, dict)
                        and answer.get("next") == before.get("destination")
                        and str(answer.get("label") or answer_id) == before.get("route")):
                    matches.append((answer, "next"))
        if len(matches) != 1:
            raise StructuralRepairPreviewError("A simulated edge no longer resolves exactly once.")
        matches[0][0][matches[0][1]] = after.get("destination")

    @classmethod
    def _collision_safe_id(cls, base: str, role: str, seed: dict[str, Any], used: set[str]) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", base):
            raise StructuralRepairPreviewError("Evidence specifications require safe proposed node IDs.")
        if base not in used:
            return base
        digest = cls._hash({"seed": seed, "role": role, "base": base})
        for length in range(8, len(digest) + 1):
            candidate = f"{base}_{digest[:length]}"
            if candidate not in used:
                return candidate
        raise StructuralRepairPreviewError("A collision-safe proposed node ID could not be generated.")

    @staticmethod
    def _workflow_edges(workflow: dict[str, Any]) -> list[dict[str, str]]:
        graph = WorkflowGraph(workflow)
        return [
            {"source": source, "route": route, "destination": destination}
            for source in workflow["nodes"]
            for route, destination in graph.transitions(source)
        ]

    @staticmethod
    def _edge_key(edge: Any) -> tuple[str, str, str]:
        if not isinstance(edge, dict):
            return "", "", ""
        return str(edge.get("source") or ""), str(edge.get("route") or ""), str(edge.get("destination") or "")

    @staticmethod
    def _task_identity(task: dict[str, Any]) -> dict[str, Any]:
        return {key: task.get(key) for key in (
            "task_id", "finding_id", "durable_identity", "curator_rule",
            "finding_type", "content_type", "content_identifier",
        )}

    @staticmethod
    def _specification_payload(specification: EvidenceProbeSpecification) -> dict[str, Any]:
        return to_plain_data(asdict(specification))

    @staticmethod
    def _hash(value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
