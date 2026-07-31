import json
from pathlib import Path


class DecisionEngine:
    """Loads and navigates SupportPilot decision tree workflows."""

    def __init__(self):
        self.workflow = None

    def load_workflow(self, workflow_name):
        """Load a workflow JSON file."""

        workflow_path = (
            Path(__file__).parent.parent
            / "decision_trees"
            / f"{workflow_name}.json"
        )

        with open(workflow_path, "r", encoding="utf-8") as file:
            self.workflow = json.load(file)

    def get_start_node(self):
        """Return the first node in the workflow."""

        start_node = self.workflow["start_node"]
        return self.workflow["nodes"][start_node]

    def get_node(self, node_name):
        """Return a node by name."""

        return self.workflow["nodes"].get(node_name)

    def get_next_node(self, current_node, answer):
        """Return the next node based on the selected answer."""

        node = self.get_node(current_node)

        if node is None:
            return None

        next_node_name = node["answers"].get(answer)

        if next_node_name is None:
            return None

        return self.get_node(next_node_name)