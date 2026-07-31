import json
from pathlib import Path


class DecisionEngine:
    """
    Loads and navigates SupportPilot decision tree workflows.
    """

    def __init__(self):
        self.workflow = None

    def load_workflow(self, workflow_name):
        """
        Load a workflow JSON file.
        """

        workflow_path = (
            Path(__file__).parent.parent
            / "decision_trees"
            / f"{workflow_name}.json"
        )

        with open(workflow_path, "r", encoding="utf-8") as file:
            self.workflow = json.load(file)

    def get_node(self, node_id):
        """
        Return a node by its ID.
        """

        node = self.workflow["nodes"].get(node_id)

        if node is None:
            return None

        return {
            "id": node_id,
            **node
        }

    def get_start_node(self):
        """
        Return the first node in the workflow.
        """

        return self.get_node(self.workflow["start_node"])

    def get_next_node(self, current_node, answer):
        """
        Return the next node after a question is answered.
        """

        node = self.get_node(current_node)

        if node is None:
            return None

        next_node = node["answers"].get(answer)

        if next_node is None:
            return None

        return self.get_node(next_node)

    def advance(self, node, answer=None):
        """
        Advance through the workflow based on the current node type.
        """

        if node["type"] == "question":
            return self.get_next_node(node["id"], answer)

        if node["type"] == "instruction":
            return self.get_node(node["next"])

        if node["type"] == "resolution":
            return None

        return None