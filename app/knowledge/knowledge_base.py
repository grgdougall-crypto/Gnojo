import json
from pathlib import Path


class KnowledgeBase:
    """
    Loads reusable SupportPilot knowledge articles.
    """

    def __init__(self):
        self.knowledge_path = (
            Path(__file__).parent.parent.parent
            / "knowledge_base"
        )

        self.published_path = (
            self.knowledge_path
            / "published"
        )

    def load_article(self, article_id):
        """
        Load a published knowledge article by its ID.
        """

        article_path = (
            self.published_path
            / f"{article_id}.json"
        )

        if not article_path.exists():
            return None

        with open(
            article_path,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)