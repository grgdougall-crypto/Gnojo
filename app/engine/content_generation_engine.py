from functools import cached_property

from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider


class ContentGenerationEngine:
    """
    Generates structured Gnojo content drafts.
    """

    @cached_property
    def primary_provider(self):
        """Construct Gemini only when command generation is requested."""
        return GeminiProvider()

    @cached_property
    def fallback_provider(self):
        """Construct OpenAI only when the Gemini attempt needs a fallback."""
        return OpenAIProvider()

    def generate_command(
        self,
        command_name,
        description="",
    ):
        """
        Generate a command using Gemini first,
        then fall back to OpenAI if Gemini fails.
        """

        provider_name = "Gemini"

        try:
            generated = (
                self.primary_provider.generate_command(
                    command_name,
                    description,
                )
            )

        except Exception:
            provider_name = "OpenAI"

            generated = (
                self.fallback_provider.generate_command(
                    command_name,
                    description,
                )
            )

        generated["command_name"] = (
            command_name.strip().lower()
        )

        generated["description"] = (
            description.strip()
        )

        generated["status"] = (
            "Generated Draft"
        )

        generated["generation_provider"] = (
            provider_name
        )

        return generated
