from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from app.services.curator_structural_repair_contracts import (
    ActionVerificationRepairPlan,
    ActionVerificationSpecification,
    EvidenceProbeSpecification,
    ProgressMetadataRepairPlan,
    ProgressMetadataSpecification,
    StructuralRepairContractError,
    StructuralRepairPlan,
    to_plain_data,
)
from app.services.workflow_validation_service import WorkflowValidationService
from app.services.curator_structural_repair_governance import StructuralRepairFingerprint
from curator.workflow_reasoning import WorkflowGraph
from curator.workflow_reasoning import WorkflowReasoningAuditor


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

    def preview(self, task: dict[str, Any], specification: Any,
                workflow: dict[str, Any]) -> dict[str, Any]:
        if (task.get("curator_rule") == "CUR-WR-ACTION-VERIFICATION"
                or task.get("finding_type") == "workflow_reasoning_unverified_action"):
            if not isinstance(specification, ActionVerificationSpecification):
                return {
                    "available": False,
                    "reason": "An approved action-verification specification is required.",
                    "read_only": True,
                }
            return self._preview_action_verification(task, specification, workflow)
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

    def _preview_action_verification(
        self,
        task: dict[str, Any],
        specification: ActionVerificationSpecification,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            plan = self._build_action_verification(task, specification, workflow)
        except StructuralRepairPreviewError as error:
            return {"available": False, "reason": str(error), "read_only": True}
        plan_data = to_plain_data(asdict(plan))
        before_edge = asdict(plan.outgoing_edge)
        verification_node = {
            "node_id": plan.specification.verification_node.node_id,
            "content": to_plain_data(plan.specification.verification_node.content),
        }
        result_routes = [
            {"answer": answer, "destination": destination}
            for answer, destination in plan.specification.result_routes
        ]
        proposed = {
            "inserted_nodes": [verification_node],
            "result_decision_node": verification_node,
            "outcome_nodes": [],
            "changed_predecessor_edges": [{
                "before": before_edge,
                "after": {
                    **before_edge,
                    "destination": plan.specification.verification_node.node_id,
                },
            }],
            "changed_existing_edges": [asdict(edge) for edge in plan.changed_existing_edges],
            "new_edges": [asdict(edge) for edge in plan.new_edges],
            "result_routes": result_routes,
            "unaffected_routes": [asdict(edge) for edge in plan.unaffected_routes],
            "preserved_existing_nodes": list(plan.preserved_existing_nodes),
        }
        preview = {
            "available": True,
            "read_only": True,
            "plan": plan_data,
            "specification": self._specification_payload(specification),
            "before": {
                "action_node_id": plan.action_node_id,
                "outgoing_edge": before_edge,
                "current_destination": plan.outgoing_edge.destination,
            },
            "proposed": proposed,
        }
        preview["preview_token"] = self._hash({
            "task": self._task_identity(task),
            "structured_evidence": task.get("structured_evidence"),
            "workflow": workflow,
            "specification": preview["specification"],
            "validated_plan": plan_data,
        })
        try:
            simulated = self.simulate(workflow, preview)
            preview["validation"] = self._validate_action_simulation(
                task, workflow, simulated
            )
        except StructuralRepairPreviewError as error:
            return {"available": False, "reason": str(error), "read_only": True}
        return preview

    def preview_progress_metadata(
        self,
        task: dict[str, Any],
        specification: ProgressMetadataSpecification | None,
        workflow: dict[str, Any],
        *,
        workflow_raw_sha256: str,
        workflow_semantic_sha256: str,
    ) -> dict[str, Any]:
        try:
            plan = self._build_progress_metadata(
                task, specification, workflow,
                workflow_raw_sha256=workflow_raw_sha256,
                workflow_semantic_sha256=workflow_semantic_sha256,
            )
        except StructuralRepairPreviewError as error:
            return {"available": False, "reason": str(error), "read_only": True}
        plan_data = to_plain_data(asdict(plan))
        change = {
            "path": plan.metadata_path,
            "before_present": plan.before_present,
            "before_value": plan.before_value,
            "after_value": plan.after_value,
        }
        preview = {
            "available": True,
            "read_only": True,
            "plan": plan_data,
            "specification": self._specification_payload(specification),
            "before": {"metadata": change},
            "proposed": {
                "metadata_changes": [change],
                "inserted_nodes": [],
                "changed_predecessor_edges": [],
                "changed_existing_edges": [],
                "new_edges": [],
                "unaffected_routes": self._workflow_edges(workflow),
                "preserved_existing_nodes": sorted(workflow["nodes"]),
            },
        }
        preview["preview_token"] = self._hash({
            "task": self._task_identity(task),
            "structured_evidence": task.get("structured_evidence"),
            "workflow": workflow,
            "specification": preview["specification"],
            "validated_plan": plan_data,
        })
        try:
            simulated = self.simulate(workflow, preview)
            preview["validation"] = self._validate_progress_metadata_simulation(
                task, workflow, simulated, plan
            )
        except StructuralRepairPreviewError as error:
            return {"available": False, "reason": str(error), "read_only": True}
        return preview

    def _build_progress_metadata(
        self,
        task: dict[str, Any],
        specification: ProgressMetadataSpecification | None,
        workflow: dict[str, Any],
        *,
        workflow_raw_sha256: str,
        workflow_semantic_sha256: str,
    ) -> ProgressMetadataRepairPlan:
        if (task.get("curator_rule") != "CUR-WR-PROGRESS"
                or task.get("finding_type") != "workflow_reasoning_progress_inconsistency"
                or task.get("content_type") != "workflow"):
            raise StructuralRepairPreviewError("The task is not a workflow progress finding.")
        if not isinstance(specification, ProgressMetadataSpecification) or not specification.approved:
            raise StructuralRepairPreviewError(
                "An approved progress metadata specification is required."
            )
        if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict):
            raise StructuralRepairPreviewError("The editable workflow is unavailable or malformed.")
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id or task.get("content_identifier") != workflow_id:
            raise StructuralRepairPreviewError(
                "The task and editable workflow identities do not match."
            )
        semantic = StructuralRepairFingerprint.semantic_workflow(workflow)
        if (workflow_semantic_sha256 != semantic
                or not workflow_raw_sha256):
            raise StructuralRepairPreviewError(
                "Exact raw and semantic workflow fingerprints are required."
            )
        structural = task.get("structured_evidence")
        configured = workflow.get("estimated_steps")
        maximum = WorkflowGraph(workflow).max_user_visible_path()
        if (not isinstance(structural, dict)
                or structural.get("configured_steps") != configured
                or structural.get("maximum_user_visible_nodes") != maximum
                or isinstance(configured, bool) or not isinstance(configured, int)
                or maximum <= configured):
            raise StructuralRepairPreviewError(
                "The recorded progress evidence is stale or no longer defective."
            )
        before_present = "progress_mode" in workflow
        before_value = workflow.get("progress_mode") if before_present else None
        if (not before_present and before_value is None):
            pass
        elif before_value == "static":
            pass
        elif before_value == "branch_aware":
            raise StructuralRepairPreviewError(
                "Branch-aware progress is already enabled; no repair is available."
            )
        else:
            raise StructuralRepairPreviewError(
                "The current progress mode is outside the approved metadata allowlist."
            )
        plan_seed = {
            "workflow_id": workflow_id,
            "task": self._task_identity(task),
            "specification": [specification.specification_id, specification.version],
            "raw": workflow_raw_sha256,
            "semantic": workflow_semantic_sha256,
        }
        plan_data = {
            "plan_id": "SRP-" + self._hash(plan_seed)[:12].upper(),
            "plan_type": "workflow_metadata",
            "workflow_id": workflow_id,
            "task_id": task.get("task_id"),
            "finding_id": task.get("finding_id"),
            "metadata_path": "/progress_mode",
            "before_present": before_present,
            "before_value": before_value,
            "after_value": "branch_aware",
            "workflow_raw_sha256_before": workflow_raw_sha256,
            "workflow_semantic_sha256_before": workflow_semantic_sha256,
            "unchanged_graph_sha256": StructuralRepairFingerprint.contract(workflow["nodes"]),
            "unchanged_graph_guarantee": True,
            "specification": self._specification_payload(specification),
            "expected_post_repair": {
                "rule": "CUR-WR-PROGRESS", "status": "finding_absent",
            },
        }
        try:
            return ProgressMetadataRepairPlan.from_dict(plan_data)
        except StructuralRepairContractError as error:
            raise StructuralRepairPreviewError(str(error)) from error

    @staticmethod
    def _validate_progress_metadata_simulation(
        task: dict[str, Any], before: dict[str, Any], simulated: dict[str, Any],
        plan: ProgressMetadataRepairPlan,
    ) -> dict[str, Any]:
        if StructuralRepairFingerprint.contract(before.get("nodes")) != plan.unchanged_graph_sha256:
            raise StructuralRepairPreviewError("The current workflow graph fingerprint is stale.")
        if before.get("nodes") != simulated.get("nodes"):
            raise StructuralRepairPreviewError("A metadata preview changed the workflow graph.")
        expected = deepcopy(before)
        expected["progress_mode"] = "branch_aware"
        if simulated != expected or simulated.get("estimated_steps") != before.get("estimated_steps"):
            raise StructuralRepairPreviewError(
                "The preview contains a mutation outside the approved progress-mode field."
            )
        validation = WorkflowValidationService().validate(simulated)
        quality = validation.get("quality") or {}
        quality_rules = {item.get("rule") for item in quality.get("findings", [])}
        prohibited = {
            "PREMATURE_STATIC_PROGRESS",
            "STATIC_PATH_LENGTH_CONFLICT",
            "BRANCH_PROGRESS_INTEGRITY",
        }
        if (not validation.get("is_valid")
                or quality.get("overall_status") != "CLEAN"
                or quality_rules & prohibited):
            raise StructuralRepairPreviewError(
                "The simulated branch-aware workflow does not validate cleanly."
            )
        auditor = WorkflowReasoningAuditor()
        before_findings = auditor.analyze(before)
        after_findings = auditor.analyze(simulated)
        if any(item.rule == "CUR-WR-PROGRESS" for item in after_findings):
            raise StructuralRepairPreviewError(
                "The simulated repair does not remove the progress finding."
            )
        before_signatures = {(item.rule, item.finding_type, item.node_id)
                             for item in before_findings}
        new_findings = [
            {"rule": item.rule, "finding_type": item.finding_type, "node_id": item.node_id}
            for item in after_findings
            if (item.rule, item.finding_type, item.node_id) not in before_signatures
        ]
        if new_findings:
            raise StructuralRepairPreviewError(
                "The simulated metadata repair introduces a new reasoning finding."
            )
        return {
            "passed": True,
            "schema": validation,
            "quality": quality,
            "original_finding_absent": True,
            "new_reasoning_findings": [],
            "graph_unchanged": True,
            "estimated_steps_unchanged": True,
        }

    def _build_action_verification(
        self,
        task: dict[str, Any],
        specification: ActionVerificationSpecification,
        workflow: dict[str, Any],
    ) -> ActionVerificationRepairPlan:
        if (task.get("curator_rule") != "CUR-WR-ACTION-VERIFICATION"
                or task.get("finding_type") != "workflow_reasoning_unverified_action"):
            raise StructuralRepairPreviewError(
                "The task is not a post-action verification finding."
            )
        if not specification.approved:
            raise StructuralRepairPreviewError(
                "An approved action-verification specification is required."
            )
        if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict):
            raise StructuralRepairPreviewError("The editable workflow is unavailable or malformed.")
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        task_workflow, _, task_node = str(task.get("content_identifier") or "").partition(":")
        structural = task.get("structured_evidence")
        if not isinstance(structural, dict):
            raise StructuralRepairPreviewError("Typed action-edge evidence is unavailable.")
        action_node_id = str(structural.get("action_node_id") or "")
        if (workflow_id != task_workflow
                or workflow_id != specification.workflow_id
                or task_node != action_node_id
                or action_node_id != specification.action_node_id):
            raise StructuralRepairPreviewError(
                "The task, action specification, and editable workflow identities do not match."
            )
        if (structural.get("evidence_version") != "1.0"
                or structural.get("action_node_type") != "instruction"
                or structural.get("verification_key") != specification.verification_key
                or structural.get("action_family") != specification.action_family):
            raise StructuralRepairPreviewError(
                "The action evidence does not match the reviewed specification."
            )
        action = workflow["nodes"].get(action_node_id)
        if not isinstance(action, dict) or action.get("type") != "instruction":
            raise StructuralRepairPreviewError("The recorded action node is no longer an instruction.")
        transitions = WorkflowGraph(workflow).transitions(action_node_id)
        if len(transitions) != 1:
            raise StructuralRepairPreviewError(
                "The action must have exactly one unambiguous outgoing edge."
            )
        route, destination = transitions[0]
        edge = structural.get("outgoing_edge")
        expected_edge = {
            "source": action_node_id,
            "route": route,
            "destination": destination,
        }
        if (edge != expected_edge
                or route != "next"
                or destination != specification.expected_current_destination
                or structural.get("current_destination") != destination):
            raise StructuralRepairPreviewError(
                "The recorded action edge is stale or does not match the approved topology."
            )
        if list(structural.get("required_destinations") or []) != sorted(
            set(destination for _, destination in specification.result_routes)
        ):
            raise StructuralRepairPreviewError(
                "The bounded downstream destinations do not match the specification."
            )
        verification_id = specification.verification_node.node_id
        if verification_id in workflow["nodes"]:
            raise StructuralRepairPreviewError(
                "The approved verification node already exists; no duplicate preview is allowed."
            )
        required_destinations = {
            destination for _, destination in specification.result_routes
        }
        if not required_destinations <= set(workflow["nodes"]):
            raise StructuralRepairPreviewError(
                "An approved verification destination is unavailable in the workflow."
            )
        current_edges = self._workflow_edges(workflow)
        unaffected = [item for item in current_edges if self._edge_key(item) != self._edge_key(edge)]
        plan_seed = {
            "workflow_id": workflow_id,
            "task": self._task_identity(task),
            "specification": [specification.specification_id, specification.version],
            "outgoing_edge": edge,
        }
        plan_data = {
            "plan_id": "SRP-" + self._hash(plan_seed)[:12].upper(),
            "workflow_id": workflow_id,
            "action_node_id": action_node_id,
            "verification_key": specification.verification_key,
            "outgoing_edge": deepcopy(edge),
            "specification": self._specification_payload(specification),
            "changed_existing_edges": [deepcopy(edge)],
            "new_edges": [
                {"source": verification_id, "route": answer, "destination": destination}
                for answer, destination in specification.result_routes
            ],
            "preserved_existing_nodes": sorted(workflow["nodes"]),
            "unaffected_routes": unaffected,
            "expected_post_repair": {
                "rule": "CUR-WR-ACTION-VERIFICATION",
                "status": "finding_absent",
            },
        }
        try:
            return ActionVerificationRepairPlan.from_dict(plan_data)
        except StructuralRepairContractError as error:
            raise StructuralRepairPreviewError(str(error)) from error

    @staticmethod
    def _validate_action_simulation(
        task: dict[str, Any], before: dict[str, Any], simulated: dict[str, Any],
    ) -> dict[str, Any]:
        validation = WorkflowValidationService().validate(simulated)
        quality = validation.get("quality") or {}
        if (not validation.get("is_valid")
                or validation.get("errors")
                or validation.get("warnings")
                or quality.get("overall_status") not in {None, "CLEAN"}):
            raise StructuralRepairPreviewError(
                "The simulated action-verification repair does not validate cleanly."
            )
        auditor = WorkflowReasoningAuditor()
        before_findings = auditor.analyze(before)
        after_findings = auditor.analyze(simulated)
        action_node_id = str(task.get("structured_evidence", {}).get("action_node_id") or "")
        if any(item.rule == "CUR-WR-ACTION-VERIFICATION"
               and item.node_id == action_node_id for item in after_findings):
            raise StructuralRepairPreviewError(
                "The simulated repair does not remove the action-verification finding."
            )
        before_signatures = {(item.rule, item.finding_type, item.node_id) for item in before_findings}
        new_findings = [
            {"rule": item.rule, "finding_type": item.finding_type, "node_id": item.node_id}
            for item in after_findings
            if (item.rule, item.finding_type, item.node_id) not in before_signatures
        ]
        if new_findings:
            raise StructuralRepairPreviewError(
                "The simulated repair introduces a new reasoning finding."
            )
        return {
            "passed": True,
            "schema": validation,
            "original_finding_absent": True,
            "new_reasoning_findings": [],
        }

    def simulate(self, workflow: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
        """Apply an already validated preview to an in-memory copy only."""
        if not preview.get("available") or not isinstance(preview.get("proposed"), dict):
            raise StructuralRepairPreviewError("A validated structural preview is required.")
        simulated = deepcopy(workflow)
        proposed = preview["proposed"]
        metadata_changes = proposed.get("metadata_changes")
        if metadata_changes is not None:
            if (not isinstance(metadata_changes, list) or len(metadata_changes) != 1
                    or metadata_changes[0].get("path") != "/progress_mode"
                    or metadata_changes[0].get("after_value") != "branch_aware"):
                raise StructuralRepairPreviewError(
                    "A simulated metadata change exceeds the approved allowlist."
                )
            change = metadata_changes[0]
            present = "progress_mode" in simulated
            if (present != change.get("before_present")
                    or simulated.get("progress_mode") != change.get("before_value")):
                raise StructuralRepairPreviewError("The simulated metadata before-state is stale.")
            simulated["progress_mode"] = "branch_aware"
            return simulated
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
    def _specification_payload(specification: Any) -> dict[str, Any]:
        return to_plain_data(asdict(specification))

    @staticmethod
    def _hash(value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
