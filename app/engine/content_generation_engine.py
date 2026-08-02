from app.ai.gemini_provider import GeminiProvider


class ContentGenerationEngine:
    """
    Generates structured SupportPilot content drafts.
    """

    def __init__(self):
        self.provider = GeminiProvider()

    def generate_command(
        self,
        command_name,
        description="",
    ):
        """
        Generate a command using the configured AI provider.
        """

        generated = self.provider.generate_command(
            command_name,
            description,
        )

        generated["command_name"] = command_name.strip().lower()
        generated["description"] = description.strip()
        generated["status"] = "Generated Draft"

        return generated