"""
Purpose:
    Test the SupportPilot fallback provider.

Responsibilities:
    - Confirm that the provider is always configured.
    - Confirm that it returns a successful ProviderResult.
    - Confirm that its article passes validation.

Does NOT:
    - Call external APIs.
    - Save generated articles.
"""

from app.knowledge.article_validator import ArticleValidator
from app.knowledge.providers.fallback_provider import FallbackProvider


def test_fallback_provider() -> None:
    """
    Confirm that the fallback provider returns a valid article.
    """

    provider = FallbackProvider()

    result = provider.generate_article(
        prompt="Generate an article about checking an Ethernet cable."
    )

    print("\nFALLBACK PROVIDER TEST")

    if not provider.is_configured():
        print("FAILED")
        print("The fallback provider should always be configured.")
        return

    if not result.success:
        print("FAILED")
        print(result.error_message)
        return

    if result.content is None:
        print("FAILED")
        print("The provider returned no article content.")
        return

    validation_errors = ArticleValidator.validate(result.content)

    if validation_errors:
        print("FAILED")
        print("The generated article failed validation:")

        for validation_error in validation_errors:
            print(f"- {validation_error}")

        return

    print("PASSED")
    print(f"Provider: {result.provider_name}")
    print(f"Model: {result.model_name}")
    print(f"Article title: {result.content['title']}")
    print("The generated article passed validation.")


def main() -> None:
    """
    Run the fallback provider test.
    """

    test_fallback_provider()


if __name__ == "__main__":
    main()