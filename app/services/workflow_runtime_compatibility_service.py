from __future__ import annotations

from typing import Any


RUNTIME_HANDOFFS = {
    ("network_diagnostics", "advanced_complete"): "higher_layer_connectivity",
}


def runtime_overlay_present(workflow_id: str, workflow: dict[str, Any]) -> bool:
    """Return whether runtime adds a compatibility handoff to this snapshot."""
    nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
    if not isinstance(nodes, dict):
        return False
    for (source_workflow, node_id), _destination in RUNTIME_HANDOFFS.items():
        if workflow_id != source_workflow:
            continue
        node = nodes.get(node_id)
        if (isinstance(node, dict) and node.get("type") == "resolution"
                and not node.get("next_workflow")):
            return True
    return False


def apply_runtime_compatibility_handoffs(engine: Any, workflow_id: str) -> None:
    """Apply the existing narrowly scoped compatibility overlay in memory."""
    for (source_workflow, node_id), destination_workflow in RUNTIME_HANDOFFS.items():
        if workflow_id != source_workflow:
            continue
        node = engine.get_node(node_id)
        if node is not None and node.type == "resolution" and not node.next_workflow:
            node.next_workflow = destination_workflow
