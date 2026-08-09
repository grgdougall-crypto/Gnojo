from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.services.knowledge_identity_service import KnowledgeIdentityService


@dataclass(frozen=True)
class IdentityMatch:
    article: dict[str, Any]
    confidence: float
    method: str
    reasoning: list[str]


class ArticleIdentityResolver:
    """Conservative, deterministic identity resolution for knowledge articles."""

    THRESHOLD = 0.95

    def __init__(self, repository):
        self.repository = repository
        self.alias_path = repository.knowledge_base_directory / "aliases.json"

    def aliases(self) -> dict[str, str]:
        if not self.alias_path.exists():
            return {}
        try:
            data = json.loads(self.alias_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in (data.get("aliases") or {}).items()}

    def save_aliases(self, aliases: dict[str, str]) -> None:
        self.repository._write_json_atomic(
            self.alias_path, {"schema_version": "1.0", "aliases": dict(sorted(aliases.items()))}
        )

    def resolve(self, identifier: str | None = None, candidate: dict[str, Any] | None = None,
                include_drafts: bool = False) -> IdentityMatch | None:
        articles = self.repository.get_published()
        if include_drafts:
            articles += self.repository.get_drafts()
        return self._resolve_from_articles(identifier, candidate, articles)

    def resolve_published(self, identifier: str | None = None,
                          candidate: dict[str, Any] | None = None) -> IdentityMatch | None:
        """Resolve only against the canonical published-article index."""
        return self._resolve_from_articles(identifier, candidate, self.repository.get_published())

    def _resolve_from_articles(self, identifier: str | None, candidate: dict[str, Any] | None,
                               articles: list[dict[str, Any]]) -> IdentityMatch | None:
        identifier = str(identifier or "").strip()
        # Required enterprise lookup order.
        if identifier:
            exact = next((a for a in articles if a.get("id") == identifier), None)
            if exact:
                return IdentityMatch(exact, 1.0, "exact_article_id", ["Exact article ID match."])
            canonical = next((a for a in articles if KnowledgeIdentityService.canonical_id(a) == identifier), None)
            if canonical:
                return IdentityMatch(canonical, 1.0, "canonical_identity", ["Canonical identity ID match."])
            alias_target = self.aliases().get(identifier)
            if alias_target:
                target = next((a for a in articles if a.get("id") == alias_target), None)
                if target:
                    return IdentityMatch(target, 1.0, "alias", [f"Alias resolves to {alias_target}."])
        probe = dict(candidate or {})
        if identifier and not probe.get("title") and " " in identifier:
            probe["title"] = identifier
        title = self._norm(probe.get("title"))
        if title:
            exact_title = next((a for a in articles if self._norm(a.get("title")) == title), None)
            if exact_title:
                return IdentityMatch(exact_title, 1.0, "normalized_title", ["Normalized titles are identical."])
        scored = [(self.similarity(probe, article), article) for article in articles]
        score, article = max(scored, default=(0.0, None), key=lambda item: item[0][0])
        if article is not None and score[0] >= self.THRESHOLD:
            return IdentityMatch(article, score[0], "semantic_similarity", score[1])
        return None

    def similarity(self, left: dict[str, Any], right: dict[str, Any]) -> tuple[float, list[str]]:
        fields = {
            "title": .38, "overview": .18, "checklist": .14, "commands": .08,
            "related_topics": .07, "sources": .05, "workflow_references": .05,
        }
        total = 0.0
        reasons = []
        for field, weight in fields.items():
            a, b = self._text(left.get(field)), self._text(right.get(field))
            if not a or not b:
                continue
            ratio = SequenceMatcher(None, a, b).ratio()
            total += weight * ratio
            if ratio >= .9:
                reasons.append(f"{field.replace('_', ' ').title()} strongly matches ({ratio:.0%}).")
        canonical_left = left.get("canonical_id") or left.get("id")
        canonical_right = right.get("canonical_id") or right.get("id")
        if canonical_left and canonical_right and canonical_left == canonical_right:
            total += .05
            reasons.append("Canonical identities match.")
        # Compare only available evidence, while never allowing weak title similarity to pass.
        available = sum(weight for field, weight in fields.items() if self._text(left.get(field)) and self._text(right.get(field))) + (.05 if canonical_left and canonical_right else 0)
        normalized = total / available if available else 0.0
        if self._norm(left.get("title")) != self._norm(right.get("title")) and SequenceMatcher(None, self._norm(left.get("title")), self._norm(right.get("title"))).ratio() < .92:
            normalized = min(normalized, .94)
        return round(normalized, 4), reasons or ["No strong shared evidence was found."]

    @staticmethod
    def _norm(value: Any) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()

    @classmethod
    def _text(cls, value: Any) -> str:
        if isinstance(value, list):
            value = " ".join(json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item) for item in value)
        if isinstance(value, dict):
            value = json.dumps(value, sort_keys=True)
        return cls._norm(value)
