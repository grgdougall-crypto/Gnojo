class WorkflowOutlineService:
    """
    Converts a workflow JSON document into
    a human-readable outline.
    """

    def build_outline(self, workflow):

        nodes = workflow.get("nodes", {})
        start_node = workflow.get("start_node")

        outline = []

        visited = set()

        self._walk(
            node_id=start_node,
            nodes=nodes,
            outline=outline,
            visited=visited,
            depth=0,
        )

        return outline

    def _walk(
        self,
        node_id,
        nodes,
        outline,
        visited,
        depth,
    ):

        if node_id in visited:
            return

        node = nodes.get(node_id)

        if node is None:
            return

        visited.add(node_id)

        outline.append(
            {
                "depth": depth,
                "id": node_id,
                "type": node.get("type"),
                "title": self._title(node),
                "help_text": node.get("help_text"),
            }
        )

        if node.get("type") == "question":

            for answer, answer_data in (
                node.get("answers", {})
            ).items():

                if isinstance(answer_data, dict):
                    next_node = answer_data.get("next")
                    label = answer_data.get(
                        "label",
                        answer,
                    )
                else:
                    next_node = answer_data
                    label = answer

                outline.append(
                    {
                        "depth": depth + 1,
                        "type": "answer",
                        "title": label,
                    }
                )

                self._walk(
                    next_node,
                    nodes,
                    outline,
                    visited,
                    depth + 2,
                )

        elif node.get("type") == "instruction":

            self._walk(
                node.get("next"),
                nodes,
                outline,
                visited,
                depth + 1,
            )

    def _title(self, node):

        if node.get("type") == "question":
            return node.get("question")

        if node.get("type") == "instruction":
            return node.get("title")

        if node.get("type") == "resolution":
            return node.get("title")

        if node.get("type") == "transition":
            return node.get("title")

        return "Unknown"