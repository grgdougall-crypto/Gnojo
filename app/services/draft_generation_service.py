from app.engine.content_generation_engine import ContentGenerationEngine
from app.models.command_explanation import CommandExplanation


class DraftGenerationService:
    """
    Creates structured SupportPilot drafts before publication.
    """

    def __init__(self):
        self.engine = ContentGenerationEngine()

    def generate_command_draft(
        self,
        command_name: str,
        description: str = "",
        use_generated_content: bool = False,
    ):
        """
        Return either an empty command draft or a generated command draft.
        """

        normalized_name = command_name.strip()
        normalized_description = description.strip()

        if use_generated_content:
            generated = self.engine.generate_command(
                normalized_name,
                normalized_description,
            )

            generated["explanation"] = CommandExplanation(
                title=f"Understanding {normalized_name}",
                purpose=generated.get("summary", ""),
                when_to_use=(
                    f"Use {normalized_name} when it supports the "
                    "current troubleshooting task."
                ),
            )

            return generated

        return {
            "command_name": normalized_name,
            "description": normalized_description,
            "status": "Draft",
            "summary": "",
            "syntax": "",
            "examples": [],
            "important_fields": [],
            "common_errors": [],
            "related_commands": [],
            "related_articles": [],
            "official_references": [],
            "generation_source": "manual",
            "explanation": CommandExplanation(
                title=f"Understanding {normalized_name}",
                purpose="",
                when_to_use="",
            ),
        }

    def calculate_completeness(self, draft):
        """
        Return the percentage of important command fields that contain data.
        """

        checks = [
            bool(draft.get("summary")),
            bool(draft.get("syntax")),
            bool(draft.get("examples")),
            bool(draft.get("important_fields")),
            bool(draft.get("common_errors")),
            bool(draft.get("related_commands")),
            bool(draft.get("official_references")),
            bool(draft.get("explanation")),
        ]

        completed = sum(checks)
        total = len(checks)

        return round(
            completed / total * 100
        )