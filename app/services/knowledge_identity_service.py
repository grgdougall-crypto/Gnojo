import re
from typing import Any


class KnowledgeIdentityError(ValueError):
    pass


class KnowledgeIdentityService:
    """One identity contract shared by authoring, runtime, and Curator."""

    @staticmethod
    def canonical_id(article: dict[str, Any] | str) -> str:
        value = article if isinstance(article, str) else (
            article.get("canonical_id") or article.get("id")
        )
        value = str(value or "").strip().casefold()
        if not value or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise KnowledgeIdentityError(
                "Canonical article IDs must contain lowercase letters, numbers, and hyphens."
            )
        return value

    @classmethod
    def normalize(cls, article: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(article)
        identifier = cls.canonical_id(normalized)
        existing = normalized.get("canonical_id")
        if existing and cls.canonical_id(str(existing)) != identifier:
            raise KnowledgeIdentityError("Article identity is inconsistent.")
        normalized["id"] = identifier
        normalized["canonical_id"] = identifier
        return normalized

    @staticmethod
    def normalized_title(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
