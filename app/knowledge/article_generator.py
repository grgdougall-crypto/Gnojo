"""
Purpose:
    Generate validated Gnojo knowledge articles.

Responsibilities:
    - Request prompts from ArticlePromptBuilder.
    - Request content through ProviderRouter.
    - add provider-generation metadata.
    - Validate generated articles.
    - Return only valid articles.

Does NOT:
    - Build prompt text directly.
    - Call Gemini or OpenAI directly.
    - Save articles.
    - Publish articles.
"""

from datetime import datetime, timezone
from typing import Any

from app.knowledge.article_validator import (
    ArticleValidationError,
    ArticleValidator,
)
from app.services.article_tag_service import ArticleTagService
from app.knowledge.providers.provider_router import ProviderRouter
from app.prompts.article_prompt_builder import ArticlePromptBuilder


class ArticleGenerator:
    """
    Coordinate Gnojo article generation.
    """

    def __init__(
        self,
        provider_router: ProviderRouter,
        prompt_builder: ArticlePromptBuilder,
    ) -> None:
        """
        Initialize the article generator.

        Args:
            provider_router:
                Router responsible for provider selection and failover.

            prompt_builder:
                Builder responsible for article-generation prompts.
        """

        self.provider_router = provider_router
        self.prompt_builder = prompt_builder

    def generate(
        self,
        topic: str,
        category: str,
        difficulty: str,
    ) -> dict[str, Any]:
        """
        Generate and validate a Gnojo article.

        Args:
            topic:
                Technical subject of the article.

            category:
                Gnojo knowledge category.

            difficulty:
                Intended difficulty level.

        Returns:
            dict:
                Valid Gnojo draft article.

        Raises:
            RuntimeError:
                If no article content is returned.

            ArticleValidationError:
                If the generated article fails validation.
        """

        prompt = self.prompt_builder.build(
            topic=topic,
            category=category,
            difficulty=difficulty,
        )

        provider_result = self.provider_router.generate_article(
            prompt=prompt,
        )

        if provider_result.content is None:
            raise RuntimeError(
                "The provider returned no article content."
            )

        article = provider_result.content

        article["tags"] = ArticleTagService.generate(article)

        self._apply_generation_metadata(
            article=article,
            provider_name=provider_result.provider_name,
            model_name=provider_result.model_name,
        )

        validation_errors = ArticleValidator.validate(article)

        if validation_errors:
            formatted_errors = "\n".join(
                f"- {validation_error}"
                for validation_error in validation_errors
            )

            raise ArticleValidationError(
                "Generated article failed validation:\n"
                f"{formatted_errors}"
            )

        return article

    def _apply_generation_metadata(
        self,
        article: dict[str, Any],
        provider_name: str,
        model_name: str | None,
    ) -> None:
        """
        Add reliable generation metadata to an article.

        Provider-generated metadata is overwritten here so Gnojo,
        rather than the AI model, records the actual provider and time.

        Args:
            article:
                Generated Gnojo article.

            provider_name:
                Provider that successfully returned the article.

            model_name:
                Model or strategy used by that provider.
        """

        article["generation"] = {
            "provider": provider_name,
            "model": model_name,
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }
