"""
Gnojo knowledge article validator.

This module checks whether a knowledge article follows the required
Gnojo article schema before it can move through the review and
publishing process.
"""

from typing import Any

from app.knowledge.article_schema import (
    ARTICLE_SCHEMA_VERSION,
    REQUIRED_TOP_LEVEL_FIELDS,
    VALID_DIFFICULTIES,
    VALID_REVIEW_STATUSES,
)


class ArticleValidationError(Exception):
    """Raised when a knowledge article fails validation."""


class ArticleValidator:
    """
    Validate Gnojo knowledge articles.

    The validator returns a list of human-readable errors so multiple
    problems can be corrected at the same time.
    """

    @staticmethod
    def validate(article: dict[str, Any]) -> list[str]:
        errors: list[str] = []

        if not isinstance(article, dict):
            return ["Article must be a JSON object."]

        ArticleValidator._validate_required_fields(article, errors)
        ArticleValidator._validate_basic_fields(article, errors)
        ArticleValidator._validate_list_fields(article, errors)
        ArticleValidator._validate_commands(article, errors)
        ArticleValidator._validate_quiz(article, errors)
        ArticleValidator._validate_sources(article, errors)
        ArticleValidator._validate_generation(article, errors)
        ArticleValidator._validate_review(article, errors)

        return errors

    @staticmethod
    def validate_or_raise(article: dict[str, Any]) -> None:
        """
        Validate an article and raise an exception when errors are found.
        """

        errors = ArticleValidator.validate(article)

        if errors:
            formatted_errors = "\n".join(
                f"- {error}" for error in errors
            )

            raise ArticleValidationError(
                f"Article validation failed:\n{formatted_errors}"
            )

    @staticmethod
    def is_valid(article: dict[str, Any]) -> bool:
        """
        Return True when an article passes validation.
        """

        return len(ArticleValidator.validate(article)) == 0

    @staticmethod
    def _validate_required_fields(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        missing_fields = REQUIRED_TOP_LEVEL_FIELDS - article.keys()

        for field in sorted(missing_fields):
            errors.append(f"Missing required field: '{field}'.")

    @staticmethod
    def _validate_basic_fields(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        schema_version = article.get("schema_version")

        if schema_version != ARTICLE_SCHEMA_VERSION:
            errors.append(
                "Field 'schema_version' must be "
                f"'{ARTICLE_SCHEMA_VERSION}'."
            )

        ArticleValidator._require_nonempty_string(
            article,
            "id",
            errors,
        )

        ArticleValidator._require_nonempty_string(
            article,
            "title",
            errors,
        )

        ArticleValidator._require_nonempty_string(
            article,
            "category",
            errors,
        )

        ArticleValidator._require_nonempty_string(
            article,
            "estimated_time",
            errors,
        )

        ArticleValidator._require_nonempty_string(
            article,
            "overview",
            errors,
        )

        difficulty = article.get("difficulty")

        if difficulty not in VALID_DIFFICULTIES:
            valid_values = ", ".join(sorted(VALID_DIFFICULTIES))

            errors.append(
                f"Field 'difficulty' must be one of: {valid_values}."
            )

    @staticmethod
    def _validate_list_fields(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        list_fields = [
            "checklist",
            "common_indicators",
            "commands",
            "related_topics",
            "quiz",
            "sources",
        ]

        for field in list_fields:
            value = article.get(field)

            if not isinstance(value, list):
                errors.append(f"Field '{field}' must be a list.")

        string_list_fields = [
            "checklist",
            "common_indicators",
            "related_topics",
        ]

        for field in string_list_fields:
            value = article.get(field)

            if not isinstance(value, list):
                continue

            for index, item in enumerate(value):
                if not isinstance(item, str) or not item.strip():
                    errors.append(
                        f"Field '{field}' item {index + 1} "
                        "must be a non-empty string."
                    )

    @staticmethod
    def _validate_commands(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        commands = article.get("commands")

        if not isinstance(commands, list):
            return

        for index, command in enumerate(commands):
            location = f"commands item {index + 1}"

            if not isinstance(command, dict):
                errors.append(
                    f"{location} must be an object."
                )
                continue

            required_fields = {
                "command",
                "description",
            }

            for field in required_fields:
                value = command.get(field)

                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{location} field '{field}' "
                        "must be a non-empty string."
                    )

    @staticmethod
    def _validate_quiz(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        quiz = article.get("quiz")

        if not isinstance(quiz, list):
            return

        for index, question in enumerate(quiz):
            location = f"quiz item {index + 1}"

            if not isinstance(question, dict):
                errors.append(
                    f"{location} must be an object."
                )
                continue

            question_text = question.get("question")

            if (
                not isinstance(question_text, str)
                or not question_text.strip()
            ):
                errors.append(
                    f"{location} field 'question' "
                    "must be a non-empty string."
                )

            answers = question.get("answers")

            if not isinstance(answers, list) or len(answers) < 2:
                errors.append(
                    f"{location} field 'answers' "
                    "must contain at least two answers."
                )
                continue

            for answer_index, answer in enumerate(answers):
                if not isinstance(answer, str) or not answer.strip():
                    errors.append(
                        f"{location} answer {answer_index + 1} "
                        "must be a non-empty string."
                    )

            correct_answer = question.get("correct_answer")

            if (
                not isinstance(correct_answer, str)
                or not correct_answer.strip()
            ):
                errors.append(
                    f"{location} field 'correct_answer' "
                    "must be a non-empty string."
                )
            elif correct_answer not in answers:
                errors.append(
                    f"{location} field 'correct_answer' "
                    "must match one of the provided answers."
                )

    @staticmethod
    def _validate_sources(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        sources = article.get("sources")

        if not isinstance(sources, list):
            return

        for index, source in enumerate(sources):
            location = f"sources item {index + 1}"

            if not isinstance(source, dict):
                errors.append(
                    f"{location} must be an object."
                )
                continue

            title = source.get("title")
            url = source.get("url")

            if not isinstance(title, str) or not title.strip():
                errors.append(
                    f"{location} field 'title' "
                    "must be a non-empty string."
                )

            if not isinstance(url, str) or not url.strip():
                errors.append(
                    f"{location} field 'url' "
                    "must be a non-empty string."
                )

    @staticmethod
    def _validate_generation(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        generation = article.get("generation")

        if not isinstance(generation, dict):
            errors.append(
                "Field 'generation' must be an object."
            )
            return

        required_fields = {
            "provider",
            "model",
            "generated_at",
        }

        missing_fields = required_fields - generation.keys()

        for field in sorted(missing_fields):
            errors.append(
                f"Field 'generation' is missing '{field}'."
            )

    @staticmethod
    def _validate_review(
        article: dict[str, Any],
        errors: list[str],
    ) -> None:
        review = article.get("review")

        if not isinstance(review, dict):
            errors.append(
                "Field 'review' must be an object."
            )
            return

        required_fields = {
            "status",
            "reviewed_by",
            "reviewed_at",
            "notes",
        }

        missing_fields = required_fields - review.keys()

        for field in sorted(missing_fields):
            errors.append(
                f"Field 'review' is missing '{field}'."
            )

        status = review.get("status")

        if status not in VALID_REVIEW_STATUSES:
            valid_values = ", ".join(
                sorted(VALID_REVIEW_STATUSES)
            )

            errors.append(
                "Field 'review.status' must be one of: "
                f"{valid_values}."
            )

        notes = review.get("notes")

        if not isinstance(notes, list):
            errors.append(
                "Field 'review.notes' must be a list."
            )

    @staticmethod
    def _require_nonempty_string(
        article: dict[str, Any],
        field: str,
        errors: list[str],
    ) -> None:
        value = article.get(field)

        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"Field '{field}' must be a non-empty string."
            )