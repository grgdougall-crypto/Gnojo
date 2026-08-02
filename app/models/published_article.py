from dataclasses import dataclass

from app.models.command_explanation import CommandExplanation
from app.models.draft_metadata import DraftMetadata


@dataclass
class PublishedArticle:
    """
    Represents a published knowledge article.
    """

    command_name: str
    description: str
    summary: str
    syntax: str

    examples: list
    important_fields: list
    common_errors: list
    related_commands: list
    official_references: list

    explanation: CommandExplanation

    metadata: DraftMetadata