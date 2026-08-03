class WorkflowNodeService:
    """
    Builds a human-friendly list of workflow nodes.
    """

    def build(self, workflow):

        nodes = workflow.get(
            "nodes",
            {},
        )

        results = []

        for node_id, node in nodes.items():

            results.append(
                {
                    "id": node_id,
                    "type": node.get("type"),
                    "title": self._title(node),
                }
            )

        return sorted(
            results,
            key=lambda node: (
                node["type"],
                node["title"],
            ),
        )

    def _title(self, node):

        if node.get("type") == "question":
            return node.get(
                "question",
                "Untitled Question",
            )

        return node.get(
            "title",
            "Untitled",
        )