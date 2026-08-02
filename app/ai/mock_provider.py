from app.ai.provider import AIProvider


class MockProvider(AIProvider):
    """
    Temporary AI provider used during development.

    Later this will be replaced with OpenAI, Claude,
    Azure OpenAI, or another provider.
    """

    def generate_command(
        self,
        command_name,
        description="",
    ):
        """
        Generate predictable draft content.
        """

        command = command_name.strip().lower()

        return {
            "summary": (
                f"{command} is a Windows command used for "
                "troubleshooting and administration."
            ),
            "syntax": f"{command} [options]",
            "examples": [
                {
                    "command": command,
                    "description": (
                        f"Runs the basic {command} command."
                    ),
                }
            ],
            "important_fields": [],
            "common_errors": [],
            "related_commands": [],
            "related_articles": [],
            "official_references": [],
            "generation_source": "mock_ai",
        }