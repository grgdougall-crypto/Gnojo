class ContentGenerationEngine:
    """
    Generates structured SupportPilot content drafts.

    This first version uses deterministic placeholder data.
    A real AI provider can be connected later without changing
    the builder route or template.
    """

    def generate_command(self, command_name, description=""):
        """
        Return a structured generated command draft.
        """

        normalized_name = command_name.strip().lower()
        normalized_description = description.strip()

        return {
            "command_name": normalized_name,
            "description": normalized_description,
            "status": "Generated Draft",
            "summary": (
                f"{normalized_name} is a command that will be documented "
                "through the SupportPilot command-generation workflow."
            ),
            "syntax": f"{normalized_name} [options]",
            "examples": [
                {
                    "command": normalized_name,
                    "description": (
                        f"Runs the basic {normalized_name} command."
                    ),
                }
            ],
            "important_fields": [],
            "common_errors": [],
            "related_commands": [],
            "related_articles": [],
            "official_references": [],
            "generation_source": "rule_engine",
        }