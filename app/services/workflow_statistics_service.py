class WorkflowStatisticsService:
    """
    Calculates useful statistics for a workflow.
    """

    def build(self, workflow):

        nodes = workflow.get(
            "nodes",
            {},
        )

        stats = {
            "total_nodes": len(nodes),
            "questions": 0,
            "instructions": 0,
            "resolutions": 0,
            "transitions": 0,
            "start_node_title": "",
        }

        for node in nodes.values():

            node_type = node.get("type")

            if node_type == "question":
                stats["questions"] += 1

            elif node_type == "instruction":
                stats["instructions"] += 1

            elif node_type == "resolution":
                stats["resolutions"] += 1

            elif node_type == "transition":
                stats["transitions"] += 1

        start_node_id = workflow.get(
            "start_node"
        )

        start_node = nodes.get(
            start_node_id
        )

        if start_node:

            if start_node.get("type") == "question":

                stats["start_node_title"] = (
                    start_node.get(
                        "question",
                        start_node_id,
                    )
                )

            else:

                stats["start_node_title"] = (
                    start_node.get(
                        "title",
                        start_node_id,
                    )
                )

        return stats