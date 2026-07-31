import json
from pathlib import Path

from app.models.node import Node


class DecisionEngine:
    """
    Loads and executes SupportPilot workflows.
    """

    def __init__(self):
        self.workflow = None

    def load_workflow(self, workflow_name):

        workflow_path = (
            Path(__file__).parent.parent
            / "decision_trees"
            / f"{workflow_name}.json"
        )

        with open(workflow_path, "r", encoding="utf-8") as file:
            self.workflow = json.load(file)

    def get_node(self, node_id):

        node = self.workflow["nodes"].get(node_id)

        if node is None:
            return None

        return Node(
            id=node_id,
            type=node["type"],
            question=node.get("question"),
            title=node.get("title"),
            instruction=node.get("instruction"),
            message=node.get("message"),
            help_text=node.get("help_text"),
            answers=node.get("answers"),
            next=node.get("next"),
        )

    def get_start_node(self):

        return self.get_node(self.workflow["start_node"])

    def get_next_node(self, current_node, answer):

        node = self.get_node(current_node)

        if node is None:
            return None

        next_node = node.answers.get(answer)

        if next_node is None:
            return None

        return self.get_node(next_node)

    def advance(self, node, answer=None):

        if node.type == "question":
            return self.get_next_node(node.id, answer)

        if node.type == "instruction":
            return self.get_node(node.next)

        if node.type == "resolution":
            return None

        return None