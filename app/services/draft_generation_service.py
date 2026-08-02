from app.engine.content_generation_engine import ContentGenerationEngine
from app.models.command_explanation import CommandExplanation
from app.models.draft_metadata import DraftMetadata


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
        Return either a blank command draft or an AI-generated draft.
        """

        normalized_name = command_name.strip()
        normalized_description = description.strip()

        if use_generated_content:
            generated = self.engine.generate_command(
                normalized_name,
                normalized_description,
            )

            explanation_data = generated.get(
                "explanation",
                {},
            )

            generated["explanation"] = CommandExplanation(
                title=explanation_data.get(
                    "title",
                    f"Understanding {normalized_name}",
                ),
                purpose=explanation_data.get(
                    "purpose",
                    generated.get("summary", ""),
                ),
                when_to_use=explanation_data.get(
                    "when_to_use",
                    "",
                ),
                what_to_check=explanation_data.get(
                    "what_to_check",
                    [],
                ),
                interpretation=explanation_data.get(
                    "interpretation",
                    [],
                ),
                common_mistake=explanation_data.get(
                    "common_mistake",
                    "",
                ),
                requires_elevation=explanation_data.get(
                    "requires_elevation",
                    False,
                ),
                permissions_notes=explanation_data.get(
                    "permissions_notes",
                    "",
                ),
                risk_level=explanation_data.get(
                    "risk_level",
                    "Unknown",
                ),
                risk_warning=explanation_data.get(
                    "risk_warning",
                    "",
                ),
                next_steps=explanation_data.get(
                    "next_steps",
                    [],
                ),
                narrative=explanation_data.get(
                    "narrative",
                    "",
                ),
            )
            generated["metadata"] = DraftMetadata()
            generated["metadata"].touch()
            return generated

        metadata = DraftMetadata()
        metadata.touch()

        return {
            "command_name": normalized_name,
            "description": normalized_description,
            "metadata": metadata,
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

        explanation = draft.get("explanation")

        explanation_complete = bool(
            explanation
            and explanation.purpose
            and explanation.when_to_use
            and explanation.narrative
        )

        checks = [
            bool(draft.get("summary")),
            bool(draft.get("syntax")),
            bool(draft.get("examples")),
            bool(draft.get("important_fields")),
            bool(draft.get("common_errors")),
            bool(draft.get("related_commands")),
            bool(draft.get("official_references")),
            explanation_complete,
        ]

        completed = sum(checks)
        total = len(checks)

        return round(
            completed / total * 100
        )