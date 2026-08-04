import json
from pathlib import Path


class KnowledgeBase:
    """
    Loads reusable Gnojo knowledge articles.
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

        try:
            with article_path.open("r", encoding="utf-8") as file:
                article = json.load(file)
        except (OSError, json.JSONDecodeError):
            return None
        return article if isinstance(article, dict) else None
