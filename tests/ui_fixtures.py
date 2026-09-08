import json
from pathlib import Path


def write_ui_workflow(root: Path, filename: str = "vpn_connectivity_win.json") -> Path:
    """Create the minimal real draft used by cross-cutting UI render tests."""
    drafts = Path(root) / "app" / "workflow_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / filename
    path.write_text(
        json.dumps({
            "workflow_id": "vpn_connectivity_win",
            "name": "VPN Connectivity",
            "description": "Deterministic workflow fixture for UI rendering.",
            "start_node": "check_connection",
            "estimated_steps": 2,
            "progress_mode": "branch_aware",
            "nodes": {
                "check_connection": {
                    "type": "question",
                    "question": "Can the VPN connect?",
                    "options": [
                        {"label": "Yes", "next": "connected"},
                        {"label": "No", "next": "not_connected"},
                    ],
                },
                "connected": {
                    "type": "resolution",
                    "title": "VPN connected",
                    "message": "The test connection completed.",
                },
                "not_connected": {
                    "type": "resolution",
                    "title": "VPN did not connect",
                    "message": "Record the test result for further review.",
                },
            },
        }, indent=2),
        encoding="utf-8",
    )
    return path
