"""
Purpose:
    Build versioned prompts for Gnojo knowledge articles.

Responsibilities:
    - Define article-generation instructions.
    - Produce consistent prompts for all AI providers.
    - Track the prompt version used for generation.
    - Validate required prompt inputs.

Does NOT:
    - Call AI providers.
    - Define the JSON response schema.
    - Validate generated articles.
    - Save or publish articles.
"""

from typing import Any


class ArticlePromptBuilder:
    """
    Build prompts for structured Gnojo article generation.
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
                Gnojo knowledge category.

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

        prompt = f"""
You are creating a learning article for Gnojo, an AI-assisted
IT troubleshooting and training platform.

PROMPT VERSION:
{self.PROMPT_VERSION}

Topic:
{topic}

Category:
{category}

Difficulty:
{difficulty}

Audience:
Entry-level IT support technicians and learners.

Requirements:
- Use clear, practical, technically accurate language.
- Create an ordered troubleshooting checklist.
- Include common symptoms or indicators.
- Include useful commands only when appropriate.
- Explain every command in plain language.
- Include at least one multiple-choice quiz question.
- Prefer official vendor documentation for sources.
- Do not invent commands or source URLs.
- Use schema version 1.0.
- Use a lowercase hyphen-separated article ID.
- Keep review status set to draft.
- Set reviewed_by and reviewed_at to null.
- Do not claim that a human reviewed the article.
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