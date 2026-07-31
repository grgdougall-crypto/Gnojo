"""
Purpose:
    Test the complete SupportPilot article-generation pipeline.

Responsibilities:
    - Build an article prompt.
    - Route generation through the fallback provider.
    - Apply generation metadata.
    - Validate the final article.

Does NOT:
    - Call external AI APIs.
    - Save or publish articles.
"""

from app.knowledge.article_generator import ArticleGenerator
from app.knowledge.providers.fallback_provider import FallbackProvider
from app.knowledge.providers.provider_router import ProviderRouter
from app.prompts.article_prompt_builder import ArticlePromptBuilder


def test_article_generator() -> None:
    """
    Confirm that the complete local generation pipeline works.
    """

    provider_router = ProviderRouter(
        providers=[
            FallbackProvider(),
        ]
    )

    prompt_builder = ArticlePromptBuilder()

    article_generator = ArticleGenerator(
        provider_router=provider_router,
        prompt_builder=prompt_builder,
    )

    article = article_generator.generate(
        topic="Check an Ethernet connection",
        category="Networking",
        difficulty="Beginner",
    )

    print("\nARTICLE GENERATOR TEST")
    print("PASSED")
    print(f"Title: {article['title']}")
    print(
        "Provider: "
        f"{article['generation']['provider']}"
    )
    print(
        "Model: "
        f"{article['generation']['model']}"
    )
    print(
        "Generated at: "
        f"{article['generation']['generated_at']}"
    )


def main() -> None:
    """
    Run the article generator test.
    """

    test_article_generator()


if __name__ == "__main__":
    main()