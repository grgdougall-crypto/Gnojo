from dataclasses import asdict
from re import sub


class PublicationMapper:
    """
    Convert published article models into repository dictionaries.
    """

    def to_repository_article(
    self,
    article,
    category="Networking",
):
        """
        Return a JSON-serializable repository article.
        """

        article_id = self._slugify(
            article.command_name
        )

        return {
            "id": article_id,
            "title": (
                f"Understanding "
                f"{article.command_name}"
            ),
            "command_name": article.command_name,
            "type": "command",
            "category": category,
            "difficulty": "Beginner",
            "estimated_time": "5–10 minutes",
            "tags": [
                article.command_name.lower(),
                "networking",
                "command prompt",
                "troubleshooting",
            ],
            "status": "published",
            "description": (
                article.description
                if article.description
                else article.summary
            ),
            "summary": article.summary,
            "syntax": article.syntax,
            "examples": article.examples,
            "important_fields": (
                article.important_fields
            ),
            "common_errors": article.common_errors,
            "related_commands": (
                article.related_commands
            ),
            "related_articles": [],
            "official_references": (
                article.official_references
            ),
            "explanation": asdict(
                article.explanation
            ),
            "metadata": asdict(
                article.metadata
            ),
            "review": {
                "status": "published",
            },
        }

    def _slugify(self, value):
        """
        Convert a title into a safe article ID.
        """

        normalized = value.strip().lower()

        normalized = sub(
            r"[^a-z0-9]+",
            "-",
            normalized,
        )

        return normalized.strip("-")