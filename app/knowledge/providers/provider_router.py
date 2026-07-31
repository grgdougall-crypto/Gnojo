"""
Purpose:
    Select the first available content provider.

Responsibilities:
    - Maintain provider priority.
    - Attempt providers in order.
    - Return the first successful result.

Does NOT:
    - Generate prompts.
    - Validate articles.
    - Save articles.
"""

from app.knowledge.providers.base_provider import (
    BaseProvider,
    ProviderError,
    ProviderResult,
)


class ProviderRouter:
    """
    Route generation requests through the configured providers.
    """

    def __init__(self, providers: list[BaseProvider]) -> None:
        self.providers = providers

    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:

        last_error = None

        for provider in self.providers:

            if not provider.is_configured():
                continue

            try:
                result = provider.generate_article(prompt)

                if result.success:
                    return result

            except ProviderError as error:
                last_error = error

        raise RuntimeError(
            "No provider was able to generate an article."
        ) from last_error