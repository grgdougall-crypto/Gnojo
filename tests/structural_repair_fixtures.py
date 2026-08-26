import json
from pathlib import Path


def pre_stage34_network_diagnostics_bytes() -> bytes:
    """Build the historical deficient topology without mutating the live draft."""
    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "workflow_drafts" / "network_diagnostics.json"
    )
    workflow = json.loads(source.read_text(encoding="utf-8"))
    nodes = workflow["nodes"]
    nodes["dns_result"]["answers"]["no"]["next"] = "dns_problem"
    for node_id in (
        "test_external_ip_reachability",
        "external_ip_reachability_result",
        "external_connectivity_unclear",
    ):
        nodes.pop(node_id, None)
    return (json.dumps(workflow, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
