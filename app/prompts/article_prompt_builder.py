"""
Purpose:
    Build versioned prompts for SupportPilot knowledge articles.

Responsibilities:
    - Define article-generation instructions.
    - Include the required SupportPilot JSON structure.
    - Produce consistent prompts for all AI providers.
    - Track the prompt version used for generation.

Does NOT:
    - Call AI providers.
    - Validate generated articles.
    - Save or publish articles.
"""

import json
from typing import Any

from app.knowledge.article_schema import create_article_template


class ArticlePromptBuilder:
    """
    Build prompts for structured SupportPilot article generation.
    """

    PROMPT_VERSION = "article-prompt-v1"

    def build(
        self,
        topic: str,
        category: str,
        difficulty: str,
    ) -> str:
        """
        Build a complete article-generation prompt.

        Args:
            topic:
                Technical subject the article should explain.

            category:
                SupportPilot knowledge category.

            difficulty:
                Intended learner difficulty level.

        Returns:
            str:
                Complete provider-neutral generation prompt.
        """

        self._validate_inputs(
            topic=topic,
            category=category,
            difficulty=difficulty,
        )

        article_template = create_article_template()

        schema_example = json.dumps(
            article_template,
            indent=2,
        )

        prompt = f"""
You are creating a structured learning article for SupportPilot.

SupportPilot is an AI-assisted IT troubleshooting and learning platform.
AI-generated content is always treated as a draft and must be reviewed
by a human before publication.

PROMPT VERSION:
{self.PROMPT_VERSION}

ARTICLE REQUEST

Topic:
{topic}

Category:
{category}

Difficulty:
{difficulty}

TARGET AUDIENCE

The intended reader is an entry-level IT support technician or learner.
Use clear, accurate, practical language.

CONTENT REQUIREMENTS

Create a concise technical learning article that includes:

1. A clear title.
2. A short overview.
3. A practical troubleshooting checklist.
4. Common indicators or symptoms.
5. Relevant commands when appropriate.
6. Related technical topics.
7. At least one multiple-choice quiz question.
8. Trusted technical sources.

QUALITY RULES

- Do not invent commands.
- Do not invent URLs.
- Prefer official vendor documentation.
- Explain commands in plain language.
- Keep troubleshooting steps practical and ordered.
- Do not claim the article has been human reviewed.
- Set review.status to "draft".
- Set reviewed_by and reviewed_at to null.
- Return JSON only.
- Do not return markdown.
- Do not add text before or after the JSON object.

REQUIRED JSON STRUCTURE

Use this exact top-level structure:

{schema_example}

FIELD RULES

- schema_version must remain unchanged.
- id must use lowercase words separated by hyphens.
- difficulty must be exactly:
  Beginner, Intermediate, or Advanced.
- commands must contain objects with:
  command and description.
- quiz answers must contain at least two choices.
- correct_answer must exactly match one provided answer.
- sources must contain objects with:
  title and url.
- generation.provider may initially be null.
- generation.model may initially be null.
- generation.generated_at may initially be null.
- review.status must be "draft".

Generate the requested SupportPilot article now.
"""

        return prompt.strip()

    def get_prompt_metadata(self) -> dict[str, Any]:
        """
        Return metadata describing this prompt builder.

        Returns:
            dict:
                Prompt name and version information.
        """

        return {
            "prompt_type": "knowledge_article",
            "prompt_version": self.PROMPT_VERSION,
        }

    def _validate_inputs(
        self,
        topic: str,
        category: str,
        difficulty: str,
    ) -> None:
        """
        Validate prompt input values before building the prompt.

        Raises:
            ValueError:
                If a required value is missing or unsupported.
        """

        if not isinstance(topic, str) or not topic.strip():
            raise ValueError(
                "Topic must be a non-empty string."
            )

        if not isinstance(category, str) or not category.strip():
            raise ValueError(
                "Category must be a non-empty string."
            )

        valid_difficulties = {
            "Beginner",
            "Intermediate",
            "Advanced",
        }

        if difficulty not in valid_difficulties:
            valid_values = ", ".join(
                sorted(valid_difficulties)
            )

            raise ValueError(
                "Difficulty must be one of: "
                f"{valid_values}."
            )