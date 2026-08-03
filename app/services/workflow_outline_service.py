class WorkflowOutlineService:
    """
    Converts a workflow JSON document into
    a flat, human-readable review outline.
    """

    def build_outline(self, workflow):
        nodes = workflow.get("nodes", {})
        start_node = workflow.get("start_node")

        outline = []

        for node_id, node in nodes.items():
            outline.append(
                self._build_item(
                    node_id=node_id,
                    node=node,
                    nodes=nodes,
                    is_start=node_id == start_node,
                )
            )

        return outline

    def _build_item(
        self,
        node_id,
        node,
        nodes,
        is_start=False,
    ):
        node_type = node.get("type")

        item = {
            "id": node_id,
            "type": node_type,
            "title": self._title(node),
            "help_text": node.get("help_text"),
            "instruction": node.get("instruction"),
            "message": node.get("message"),
            "knowledge_article": node.get(
                "knowledge_article"
            ),
            "next_workflow": node.get(
                "next_workflow"
            ),
            "is_start": is_start,
            "answers": [],
            "next": None,
        }

        if node_type == "question":
            for answer_id, answer_data in (
                node.get("answers") or {}
            ).items():

                if isinstance(answer_data, dict):
                    label = answer_data.get(
                        "label",
                        answer_id,
                    )
                    next_node_id = answer_data.get(
                        "next"
                    )
                else:
                    label = answer_id
                    next_node_id = answer_data

                item["answers"].append(
                    {
                        "id": answer_id,
                        "label": label,
                        "next_id": next_node_id,
                        "next_title": self._node_title(
                            next_node_id,
                            nodes,
                        ),
                    }
                )

        elif node_type == "instruction":
            next_node_id = node.get("next")

            item["next"] = {
                "id": next_node_id,
                "title": self._node_title(
                    next_node_id,
                    nodes,
                ),
            }

        return item

    def _node_title(
        self,
        node_id,
        nodes,
    ):
        if not node_id:
            return None

        node = nodes.get(node_id)

        if not isinstance(node, dict):
            return node_id

        return self._title(node)

    def _title(self, node):
        node_type = node.get("type")

        if node_type == "question":
            return (
                node.get("question")
                or "Untitled question"
            )

        if node_type == "instruction":
            return (
                node.get("title")
                or "Untitled instruction"
            )

        if node_type == "resolution":
            return (
                node.get("title")
                or "Untitled resolution"
            )

        if node_type == "transition":
            return (
                node.get("title")
                or "Untitled transition"
            )

        return "Unknown node"