from app.repositories.command_repository import CommandRepository
from app.repositories.knowledge_repository import (
    ArticleNotFoundError,
    KnowledgeRepository,
)


class RelationshipService:
    """
    Builds relationships between SupportPilot knowledge objects.
    """

    def __init__(self):
        self.knowledge = KnowledgeRepository()
        self.commands = CommandRepository()

    def related_articles_for_command(self, command_id):
        """
        Return published articles that reference a command.
        """

        matches = []

        for article in self.knowledge.get_published():
            command_ids = article.get(
                "related_commands",
                [],
            )

            if command_id in command_ids:
                matches.append(article)

        return matches

    def related_commands_for_article(self, article_id):
        """
        Return command records referenced by a published article.
        """

        try:
            article = self.knowledge.get_published_article(
                article_id
            )
        except ArticleNotFoundError:
            return []

        command_ids = article.get(
            "related_commands",
            [],
        )

        matches = []

        for command_id in command_ids:
            command = self.commands.get(
                command_id
            )

            if command is not None:
                matches.append(command)

        return matches