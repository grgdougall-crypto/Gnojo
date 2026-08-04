"""
Purpose:
    Define the common interface for Gnojo content providers.

Responsibilities:
    - Establish the methods every provider must implement.
    - Provide a consistent result format.
    - Define provider-specific exception types.

Does NOT:
    - Call Gemini.
    - Call OpenAI.
    - Select provider priority.
    - Validate or save generated articles.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    """
    Represent the result returned by a content provider.

    Attributes:
        provider_name:
            Human-readable provider identifier.

        model_name:
            Model used to generate the content.

        content:
            Structured article content returned by the provider.

        success:
            Whether generation completed successfully.

        error_code:
            Stable machine-readable error identifier.

        error_message:
            Human-readable explanation of a failure.

        metadata:
            Optional provider-specific diagnostic information.
    """

    provider_name: str
    model_name: str | None
    content: dict[str, Any] | None
    success: bool
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """
    Base exception for provider-related failures.
    """


class ProviderConfigurationError(ProviderError):
    """
    Raised when a provider is missing required configuration.
    """


class ProviderAuthenticationError(ProviderError):
    """
    Raised when a provider rejects its API credentials.
    """


class ProviderQuotaError(ProviderError):
    """
    Raised when a provider quota or credit limit is exhausted.
    """


class ProviderRateLimitError(ProviderError):
    """
    Raised when a provider temporarily limits request frequency.
    """


class ProviderTimeoutError(ProviderError):
    """
    Raised when a provider request exceeds its allowed time.
    """


class ProviderUnavailableError(ProviderError):
    """
    Raised when a provider service is temporarily unavailable.
    """


class ProviderResponseError(ProviderError):
    """
    Raised when a provider returns unusable or malformed content.
    """


class BaseProvider(ABC):
    """
    Define the required interface for all Gnojo providers.

    Every external or local provider must inherit from this class and
    implement its abstract properties and methods.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the stable name used to identify the provider.
        """

        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        """
        Return the model or generation strategy used by the provider.
        """

        raise NotImplementedError

    @abstractmethod
    def is_configured(self) -> bool:
        """
        Return whether the provider has the required configuration.
        """

        raise NotImplementedError

    @abstractmethod
    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:
        """
        Generate a structured knowledge article.

        Args:
            prompt:
                Complete generation instructions prepared by
                ArticleGenerator.

        Returns:
            ProviderResult:
                A consistent result object containing generated content
                or failure information.
        """

        raise NotImplementedError