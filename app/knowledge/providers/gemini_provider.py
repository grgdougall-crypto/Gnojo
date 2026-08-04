"""
Purpose:
    Generate structured Gnojo articles using Google Gemini.

Responsibilities:
    - Load Gemini configuration from environment variables.
    - Request output that follows the Gnojo article schema.
    - Parse Gemini's structured response.
    - Return a standard ProviderResult.
    - Translate provider failures into Gnojo exceptions.

Does NOT:
    - Build prompts.
    - Validate articles.
    - Save or publish articles.
    - Select provider priority.
"""

import json
import os
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.knowledge.providers.base_provider import (
    BaseProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


ARTICLE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {
            "type": "string",
        },
        "id": {
            "type": "string",
        },
        "title": {
            "type": "string",
        },
        "category": {
            "type": "string",
        },
        "difficulty": {
            "type": "string",
            "enum": [
                "Beginner",
                "Intermediate",
                "Advanced",
            ],
        },
        "estimated_time": {
            "type": "string",
        },
        "overview": {
            "type": "string",
        },
        "checklist": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "common_indicators": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                    },
                    "description": {
                        "type": "string",
                    },
                },
                "required": [
                    "command",
                    "description",
                ],
            },
        },
        "related_topics": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "quiz": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                    },
                    "answers": {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                    },
                    "correct_answer": {
                        "type": "string",
                    },
                },
                "required": [
                    "question",
                    "answers",
                    "correct_answer",
                ],
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                    },
                    "url": {
                        "type": "string",
                    },
                },
                "required": [
                    "title",
                    "url",
                ],
            },
        },
        "generation": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "model": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "generated_at": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
            },
            "required": [
                "provider",
                "model",
                "generated_at",
            ],
        },
        "review": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "draft",
                    ],
                },
                "reviewed_by": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "reviewed_at": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            },
            "required": [
                "status",
                "reviewed_by",
                "reviewed_at",
                "notes",
            ],
        },
    },
    "required": [
        "schema_version",
        "id",
        "title",
        "category",
        "difficulty",
        "estimated_time",
        "overview",
        "checklist",
        "common_indicators",
        "commands",
        "related_topics",
        "quiz",
        "sources",
        "generation",
        "review",
    ],
}


class GeminiProvider(BaseProvider):
    """
    Generate Gnojo draft articles with Google Gemini.
    """

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self) -> None:
        """
        Load local Gemini configuration.
        """

        load_dotenv()

        self._api_key = os.getenv(
            "GEMINI_API_KEY",
            "",
        ).strip()

        configured_model = os.getenv(
            "GEMINI_MODEL",
            self.DEFAULT_MODEL,
        ).strip()

        if configured_model:
            self._model_name = configured_model
        else:
            self._model_name = self.DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        """
        Return the stable provider name.
        """

        return "gemini"

    @property
    def model_name(self) -> str:
        """
        Return the configured Gemini model name.
        """

        return self._model_name

    def is_configured(self) -> bool:
        """
        Return whether the Gemini API key is available.
        """

        return bool(self._api_key)

    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:
        """
        Generate one structured Gnojo article.

        Args:
            prompt:
                Instructions created by ArticlePromptBuilder.

        Returns:
            ProviderResult:
                Generated article and provider metadata.
        """

        if not self.is_configured():
            raise ProviderConfigurationError(
                "GEMINI_API_KEY is not configured."
            )

        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderConfigurationError(
                "Gemini requires a non-empty prompt."
            )

        try:
            with genai.Client(api_key=self._api_key) as client:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=ARTICLE_RESPONSE_SCHEMA,
                        temperature=0.2,
                    ),
                )

        except Exception as error:
            self._raise_provider_error(error)

        response_text = getattr(
            response,
            "text",
            None,
        )

        if not isinstance(response_text, str):
            raise ProviderResponseError(
                "Gemini returned no text response."
            )

        if not response_text.strip():
            raise ProviderResponseError(
                "Gemini returned an empty response."
            )

        article = self._parse_response(response_text)

        metadata: dict[str, Any] = {
            "response_format": "application/json",
            "structured_output": True,
            "temperature": 0.2,
        }

        usage_metadata = getattr(
            response,
            "usage_metadata",
            None,
        )

        if usage_metadata is not None:
            metadata["usage_metadata"] = str(
                usage_metadata
            )

        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            content=article,
            success=True,
            metadata=metadata,
        )

    def _parse_response(
        self,
        response_text: str,
    ) -> dict[str, Any]:
        """
        Convert Gemini's JSON response into a dictionary.
        """

        try:
            parsed_content = json.loads(response_text)

        except json.JSONDecodeError as error:
            raise ProviderResponseError(
                "Gemini returned invalid JSON."
            ) from error

        if not isinstance(parsed_content, dict):
            raise ProviderResponseError(
                "Gemini must return one JSON object."
            )

        return parsed_content

    def _raise_provider_error(
        self,
        error: Exception,
    ) -> None:
        """
        Translate Gemini failures into Gnojo exceptions.
        """

        error_message = str(error)
        normalized_message = error_message.lower()

        status_code = getattr(
            error,
            "status_code",
            None,
        )

        if status_code in {401, 403}:
            raise ProviderAuthenticationError(
                "Gemini authentication failed."
            ) from error

        if status_code == 429:
            if self._indicates_quota_failure(
                normalized_message
            ):
                raise ProviderQuotaError(
                    "Gemini quota is unavailable or exhausted."
                ) from error

            raise ProviderRateLimitError(
                "Gemini temporarily rate-limited the request."
            ) from error

        if status_code in {500, 502, 503, 504}:
            raise ProviderUnavailableError(
                "Gemini is temporarily unavailable."
            ) from error

        if "timeout" in normalized_message:
            raise ProviderTimeoutError(
                "The Gemini request timed out."
            ) from error

        if self._indicates_quota_failure(
            normalized_message
        ):
            raise ProviderQuotaError(
                "Gemini quota is unavailable or exhausted."
            ) from error

        if (
            "api key" in normalized_message
            or "unauthenticated" in normalized_message
            or "permission denied" in normalized_message
        ):
            raise ProviderAuthenticationError(
                "Gemini authentication failed."
            ) from error

        raise ProviderUnavailableError(
            f"Gemini request failed: {error_message}"
        ) from error

    def _indicates_quota_failure(
        self,
        error_message: str,
    ) -> bool:
        """
        Return whether an error indicates exhausted quota.
        """

        quota_terms = {
            "quota",
            "resource exhausted",
            "billing",
            "credit",
        }

        return any(
            quota_term in error_message
            for quota_term in quota_terms
        )