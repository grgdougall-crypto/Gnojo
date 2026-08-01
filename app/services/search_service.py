from app.repositories.command_repository import CommandRepository
from app.repositories.knowledge_repository import KnowledgeRepository


class SearchService:
    """
    Searches and ranks results across SupportPilot repositories.
    """

    def __init__(self):
        self.knowledge = KnowledgeRepository()
        self.commands = CommandRepository()

    def search(self, query):
        """
        Return matching articles and commands ranked by relevance.
        """

        normalized_query = query.lower().strip()

        if not normalized_query:
            return {
                "articles": [],
                "commands": [],
            }

        return {
            "articles": self._search_articles(normalized_query),
            "commands": self._search_commands(normalized_query),
        }

    def _search_articles(self, query):
        ranked_results = []

        for article in self.knowledge.get_published():
            title = article.get("title", "")
            overview = article.get("overview", "")
            category = article.get("category", "")
            difficulty = article.get("difficulty", "")
            tags = self._safe_string_list(
                article.get("tags", [])
            )

            score = 0
            score += self._score_text(
                query,
                title,
                exact_score=100,
                starts_with_score=70,
                contains_score=50,
            )
            score += self._score_text(
                query,
                category,
                exact_score=35,
                starts_with_score=25,
                contains_score=15,
            )
            score += self._score_text(
                query,
                difficulty,
                exact_score=20,
                starts_with_score=15,
                contains_score=10,
            )
            score += self._score_text(
                query,
                overview,
                exact_score=20,
                starts_with_score=15,
                contains_score=10,
            )

            for tag in tags:
                score += self._score_text(
                    query,
                    tag,
                    exact_score=40,
                    starts_with_score=30,
                    contains_score=20,
                )

            if score > 0:
                ranked_results.append(
                    (
                        score,
                        article,
                    )
                )

        ranked_results.sort(
            key=lambda result: (
                -result[0],
                result[1].get("title", "").lower(),
            )
        )

        return [
            article
            for _, article in ranked_results
        ]

    def _search_commands(self, query):
        ranked_results = []

        for command in self.commands.get_all():
            name = command.get("name", "")
            title = command.get("title", "")
            summary = command.get("summary", "")
            category = command.get("category", "")
            shell = command.get("shell", "")
            platforms = self._safe_string_list(
                command.get("platforms", [])
            )
            tags = self._safe_string_list(
                command.get("tags", [])
            )

            score = 0
            score += self._score_text(
                query,
                name,
                exact_score=120,
                starts_with_score=90,
                contains_score=60,
            )
            score += self._score_text(
                query,
                title,
                exact_score=100,
                starts_with_score=70,
                contains_score=50,
            )
            score += self._score_text(
                query,
                summary,
                exact_score=25,
                starts_with_score=20,
                contains_score=15,
            )
            score += self._score_text(
                query,
                category,
                exact_score=35,
                starts_with_score=25,
                contains_score=15,
            )
            score += self._score_text(
                query,
                shell,
                exact_score=30,
                starts_with_score=20,
                contains_score=15,
            )

            for platform in platforms:
                score += self._score_text(
                    query,
                    platform,
                    exact_score=25,
                    starts_with_score=20,
                    contains_score=15,
                )

            for tag in tags:
                score += self._score_text(
                    query,
                    tag,
                    exact_score=40,
                    starts_with_score=30,
                    contains_score=20,
                )

            if score > 0:
                ranked_results.append(
                    (
                        score,
                        command,
                    )
                )

        ranked_results.sort(
            key=lambda result: (
                -result[0],
                result[1].get("name", "").lower(),
            )
        )

        return [
            command
            for _, command in ranked_results
        ]

    @staticmethod
    def _score_text(
        query,
        value,
        exact_score,
        starts_with_score,
        contains_score,
    ):
        """
        Score one searchable value against the query.
        """

        normalized_value = str(value).lower().strip()

        if not normalized_value:
            return 0

        if normalized_value == query:
            return exact_score

        if normalized_value.startswith(query):
            return starts_with_score

        if query in normalized_value:
            return contains_score

        return 0

    @staticmethod
    def _safe_string_list(value):
        """
        Return a list containing only string values.
        """

        if not isinstance(value, list):
            return []

        return [
            item
            for item in value
            if isinstance(item, str)
        ]