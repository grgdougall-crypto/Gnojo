"""
Purpose:
    Run the first live Gnojo article-generation pipeline.

Responsibilities:
    - Generate a real article through Gemini.
    - Validate the article.
    - Save the validated article as a draft.

Does NOT:
    - Publish or approve the article.
    - Use OpenAI.
"""

import json
from pathlib import Path

from app.knowledge.article_generator import ArticleGenerator
from app.knowledge.providers.fallback_provider import FallbackProvider
from app.knowledge.providers.gemini_provider import GeminiProvider
from app.knowledge.providers.provider_router import ProviderRouter
from app.prompts.article_prompt_builder import ArticlePromptBuilder


DRAFT_DIRECTORY = Path("knowledge_base") / "drafts"


def test_live_article_pipeline() -> None:
    """
    Generate, validate, and save one live article draft.
    """

    provider_router = ProviderRouter(
        providers=[
            GeminiProvider(),
            FallbackProvider(),
        ]
    )

    article_generator = ArticleGenerator(
        provider_router=provider_router,
        prompt_builder=ArticlePromptBuilder(),
    )

    article = article_generator.generate(
        topic="Use ipconfig to inspect Windows network configuration",
        category="Networking",
        difficulty="Beginner",
    )

    DRAFT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    article_path = DRAFT_DIRECTORY / (
        f"{article['id']}.json"
    )

    with article_path.open(
        "w",
        encoding="utf-8",
    ) as article_file:
        json.dump(
            article,
            article_file,
            indent=2,
            ensure_ascii=False,
        )

    print("\nLIVE ARTICLE PIPELINE")
    print("PASSED")
    print(f"Provider: {article['generation']['provider']}")
    print(f"Model: {article['generation']['model']}")
    print(f"Title: {article['title']}")
    print(f"Article ID: {article['id']}")
    print(f"Checklist items: {len(article['checklist'])}")
    print(f"Commands: {len(article['commands'])}")
    print(f"Quiz questions: {len(article['quiz'])}")
    print(f"Sources: {len(article['sources'])}")
    print(f"Draft saved to: {article_path}")


def main() -> None:
    """
    Run the live article pipeline.
    """

    test_live_article_pipeline()


if __name__ == "__main__":
    main()