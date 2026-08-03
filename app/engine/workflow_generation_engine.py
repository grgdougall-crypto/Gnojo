from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider


class WorkflowGenerationEngine:
    """
    Generate structured SupportPilot workflow drafts.
    """

    def __init__(self):
        self.primary_provider = GeminiProvider()
        self.fallback_provider = OpenAIProvider()

    def generate_workflow(
        self,
        workflow_name,
        description="",
        platform="Windows",
        difficulty="Beginner",
        size="Medium",
    ):
        """
        Generate a workflow using Gemini first,
        then fall back to OpenAI if Gemini fails.
        """

        provider_name = "Gemini"

        try:
            generated = (
                self.primary_provider.generate_workflow(
                    workflow_name=workflow_name,
                    description=description,
                    platform=platform,
                    difficulty=difficulty,
                    size=size,
                )
            )

        except Exception:
            provider_name = "OpenAI"

            generated = (
                self.fallback_provider.generate_workflow(
                    workflow_name=workflow_name,
                    description=description,
                    platform=platform,
                    difficulty=difficulty,
                    size=size,
                )
            )

        generated["generation_provider"] = provider_name
        generated["status"] = "Generated Draft"

        return generated