import json
from pathlib import Path

from app.models.node import Node


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
        Return a Node object by its ID.
        """

        if self.workflow is None:
            return None

        node_data = self.workflow["nodes"].get(node_id)

        if node_data is None:
            return None

        return Node(
            id=node_id,
            type=node_data["type"],
            question=node_data.get("question"),
            title=node_data.get("title"),
            instruction=node_data.get("instruction"),
            message=node_data.get("message"),
            help_text=node_data.get("help_text"),
            answers=node_data.get("answers"),
            next=node_data.get("next")
        )

    def get_start_node(self):
        """
        Return the first node in the workflow.
        """

        if self.workflow is None:
            return None

        return self.get_node(self.workflow["start_node"])

    def get_next_node(self, current_node_id, answer):
        """
        Return the next node after a question is answered.

        Supports both answer formats:

        Old format:
        "yes": "next_node"

        New format:
        "yes": {
            "label": "Yes",
            "next": "next_node"
        }
        """

        node = self.get_node(current_node_id)

        if node is None or node.type != "question":
            return None

        answers = node.answers or {}
        answer_data = answers.get(answer)

        if answer_data is None:
            return None

        if isinstance(answer_data, dict):
            next_node_id = answer_data.get("next")
        else:
            next_node_id = answer_data

        if next_node_id is None:
            return None

        return self.get_node(next_node_id)

    def advance(self, node, answer=None):
        """
        Advance through the workflow based on the current node type.
        """

        if node is None:
            return None

        if node.type == "question":
            return self.get_next_node(node.id, answer)

        if node.type == "instruction":
            if node.next is None:
                return None

            return self.get_node(node.next)

        if node.type == "resolution":
            return None

        return None