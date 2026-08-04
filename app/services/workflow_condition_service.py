class WorkflowConditionError(ValueError):
    pass


CONDITION_FIELDS = {
    "platform": {"Windows", "macOS", "Linux", "ChromeOS", "Other"},
    "device_type": {"Desktop", "Laptop", "Server", "Tablet", "Virtual machine", "Other"},
    "connection_type": {"Ethernet", "Wi-Fi", "Cellular", "VPN", "Offline", "Other"},
}


def node_matches_profile(node, profile):
    conditions = node.get("conditions")
    if not profile or not isinstance(conditions, dict) or not conditions:
        return True
    for field in CONDITION_FIELDS:
        expected = conditions.get(field)
        if expected and profile.get(field) != expected:
            return False
    return True


def resolve_applicable_node(engine, node_id, profile):
    """Follow explicit skip routes until an applicable node is reached."""
    skipped = []
    visited = set()
    current_id = node_id
    while current_id:
        if current_id in visited:
            raise WorkflowConditionError("Conditional routing contains a loop.")
        visited.add(current_id)
        raw_node = (engine.workflow.get("nodes") or {}).get(current_id)
        if not isinstance(raw_node, dict):
            return None, skipped
        if node_matches_profile(raw_node, profile):
            return engine.get_node(current_id), skipped
        skipped.append({"id": current_id, "title": raw_node.get("title") or raw_node.get("question") or current_id})
        current_id = raw_node.get("skip_to")
    return None, skipped
