class WorkflowProgressService:
    """Calculate an acyclic, branch-aware remaining path for opted-in workflows."""

    MODE = "branch_aware"

    @classmethod
    def enabled(cls, workflow):
        return isinstance(workflow, dict) and workflow.get("progress_mode") == cls.MODE

    @classmethod
    def total(cls, workflow, node_id, current_step):
        nodes = workflow.get("nodes", {}) if isinstance(workflow, dict) else {}
        remaining = cls._longest_path(nodes, node_id, frozenset())
        return max(int(current_step), int(current_step) - 1 + max(remaining, 1))

    @classmethod
    def _longest_path(cls, nodes, node_id, visited):
        if node_id in visited or node_id not in nodes:
            return 0
        node = nodes[node_id]
        next_ids = cls._next_ids(node)
        if not next_ids:
            return 1
        seen = visited | {node_id}
        return 1 + max((cls._longest_path(nodes, target, seen) for target in next_ids), default=0)

    @staticmethod
    def _next_ids(node):
        if not isinstance(node, dict):
            return []
        if node.get("type") == "question":
            answers = node.get("answers") or {}
            return [
                value.get("next") if isinstance(value, dict) else value
                for value in answers.values()
                if (value.get("next") if isinstance(value, dict) else value)
            ]
        if node.get("type") == "instruction" and node.get("next"):
            return [node["next"]]
        return []
