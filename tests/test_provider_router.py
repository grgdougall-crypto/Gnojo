"""
Purpose:
    Test Gnojo provider routing and failover behavior.

Responsibilities:
    - Confirm that the first successful provider is returned.
    - Confirm that unconfigured providers are skipped.
    - Confirm that provider errors trigger failover.
    - Confirm that the fallback provider is reached when needed.

Does NOT:
    - Call Gemini.
    - Call OpenAI.
    - Save generated articles.
"""

from typing import Any

from app.knowledge.providers.base_provider import (
    BaseProvider,
    ProviderResult,
    ProviderUnavailableError,
)
from app.knowledge.providers.fallback_provider import FallbackProvider
from app.knowledge.providers.provider_router import ProviderRouter


class SuccessfulTestProvider(BaseProvider):
    """
    Simulate a configured provider that succeeds.
    """

    def __init__(
        self,
        provider_name: str,
        model_name: str,
    ) -> None:
        self._provider_name = provider_name
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        """
        Return the test provider name.
        """

        return self._provider_name

    @property
    def model_name(self) -> str:
        """
        Return the test model name.
        """

        return self._model_name

    def is_configured(self) -> bool:
        """
        Return True because this provider is available for testing.
        """

        return True

    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:
        """
        Return a successful provider result.
        """

        content: dict[str, Any] = {
            "test_provider": self.provider_name,
            "prompt_received": prompt,
        }

        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            content=content,
            success=True,
        )


class FailingTestProvider(BaseProvider):
    """
    Simulate a configured provider that raises a recoverable error.
    """

    def __init__(
        self,
        provider_name: str,
        model_name: str,
    ) -> None:
        self._provider_name = provider_name
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        """
        Return the test provider name.
        """

        return self._provider_name

    @property
    def model_name(self) -> str:
        """
        Return the test model name.
        """

        return self._model_name

    def is_configured(self) -> bool:
        """
        Return True because this provider should be attempted.
        """

        return True

    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:
        """
        Raise a recoverable provider error.
        """

        raise ProviderUnavailableError(
            f"{self.provider_name} is unavailable."
        )


class UnconfiguredTestProvider(BaseProvider):
    """
    Simulate a provider with no available configuration.
    """

    @property
    def provider_name(self) -> str:
        """
        Return the test provider name.
        """

        return "unconfigured-test-provider"

    @property
    def model_name(self) -> str:
        """
        Return the test model name.
        """

        return "unconfigured-test-model"

    def is_configured(self) -> bool:
        """
        Return False so the router skips this provider.
        """

        return False

    def generate_article(
        self,
        prompt: str,
    ) -> ProviderResult:
        """
        Fail if the router incorrectly attempts this provider.
        """

        raise AssertionError(
            "The router attempted an unconfigured provider."
        )


def test_first_successful_provider() -> None:
    """
    Confirm that the router returns the first successful provider.
    """

    first_provider = SuccessfulTestProvider(
        provider_name="gemini-test",
        model_name="gemini-test-model",
    )

    second_provider = SuccessfulTestProvider(
        provider_name="openai-test",
        model_name="openai-test-model",
    )

    router = ProviderRouter(
        providers=[
            first_provider,
            second_provider,
            FallbackProvider(),
        ]
    )

    result = router.generate_article(
        prompt="Test the first successful provider."
    )

    print("\nFIRST SUCCESSFUL PROVIDER TEST")

    if result.provider_name != "gemini-test":
        print("FAILED")
        print(
            "Expected 'gemini-test' but received "
            f"'{result.provider_name}'."
        )
        return

    print("PASSED")
    print(f"Selected provider: {result.provider_name}")


def test_failover_to_second_provider() -> None:
    """
    Confirm that the router continues after a provider error.
    """

    failing_provider = FailingTestProvider(
        provider_name="gemini-test",
        model_name="gemini-test-model",
    )

    successful_provider = SuccessfulTestProvider(
        provider_name="openai-test",
        model_name="openai-test-model",
    )

    router = ProviderRouter(
        providers=[
            failing_provider,
            successful_provider,
            FallbackProvider(),
        ]
    )

    result = router.generate_article(
        prompt="Test failover to the second provider."
    )

    print("\nSECOND PROVIDER FAILOVER TEST")

    if result.provider_name != "openai-test":
        print("FAILED")
        print(
            "Expected 'openai-test' but received "
            f"'{result.provider_name}'."
        )
        return

    print("PASSED")
    print(f"Selected provider: {result.provider_name}")


def test_skip_unconfigured_provider() -> None:
    """
    Confirm that the router skips unconfigured providers.
    """

    unconfigured_provider = UnconfiguredTestProvider()

    successful_provider = SuccessfulTestProvider(
        provider_name="openai-test",
        model_name="openai-test-model",
    )

    router = ProviderRouter(
        providers=[
            unconfigured_provider,
            successful_provider,
            FallbackProvider(),
        ]
    )

    result = router.generate_article(
        prompt="Test skipping an unconfigured provider."
    )

    print("\nUNCONFIGURED PROVIDER TEST")

    if result.provider_name != "openai-test":
        print("FAILED")
        print(
            "Expected 'openai-test' but received "
            f"'{result.provider_name}'."
        )
        return

    print("PASSED")
    print("The unconfigured provider was skipped.")
    print(f"Selected provider: {result.provider_name}")


def test_failover_to_fallback_provider() -> None:
    """
    Confirm that the fallback provider is used last.
    """

    first_failing_provider = FailingTestProvider(
        provider_name="gemini-test",
        model_name="gemini-test-model",
    )

    second_failing_provider = FailingTestProvider(
        provider_name="openai-test",
        model_name="openai-test-model",
    )

    router = ProviderRouter(
        providers=[
            first_failing_provider,
            second_failing_provider,
            FallbackProvider(),
        ]
    )

    result = router.generate_article(
        prompt="Test failover to the fallback provider."
    )

    print("\nFALLBACK ROUTING TEST")

    if result.provider_name != "fallback":
        print("FAILED")
        print(
            "Expected 'fallback' but received "
            f"'{result.provider_name}'."
        )
        return

    print("PASSED")
    print(f"Selected provider: {result.provider_name}")
    print(f"Selected model: {result.model_name}")


def main() -> None:
    """
    Run all provider router tests.
    """

    test_first_successful_provider()
    test_failover_to_second_provider()
    test_skip_unconfigured_provider()
    test_failover_to_fallback_provider()


if __name__ == "__main__":
    main()