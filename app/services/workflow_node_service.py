class WorkflowNodeService:
    """
    Builds human-friendly workflow node data
    for the Workflow Designer.
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
                    "question": node.get("question"),
                    "instruction": node.get("instruction"),
                    "message": node.get("message"),
                    "help_text": node.get("help_text"),
                    "knowledge_article": node.get(
                        "knowledge_article"
                    ),
                    "next": node.get("next"),
                    "next_workflow": node.get(
                        "next_workflow"
                    ),
                    "answers": self._answers(
                        node.get("answers")
                    ),
                    "conditions": node.get("conditions", {}),
                    "skip_to": node.get("skip_to"),
                }
            )

        return sorted(
            results,
            key=lambda node: (
                node["type"] or "",
                node["title"] or "",
            ),
        )

    def _title(self, node):

        node_type = node.get("type")

        if node_type == "question":
            return node.get(
                "question",
                "Untitled Question",
            )

        if node_type == "instruction":
            return node.get(
                "title",
                "Untitled Instruction",
            )

        if node_type == "resolution":
            return node.get(
                "title",
                "Untitled Resolution",
            )

        if node_type == "transition":
            return node.get(
                "title",
                "Untitled Transition",
            )

        return "Untitled Node"

    def _answers(self, answers):

        if not isinstance(answers, dict):
            return []

        results = []

        for answer_id, answer_data in answers.items():

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

            results.append(
                {
                    "id": answer_id,
                    "label": label,
                    "next": next_node_id,
                }
            )

        return results
