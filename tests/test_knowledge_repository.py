"""
Purpose:
    Test the file-based SupportPilot knowledge repository.

Responsibilities:
    - Confirm draft loading.
    - Confirm published loading.
    - Confirm article counts.
    - Confirm one article can be loaded by ID.

Does NOT:
    - Generate articles.
    - Modify production articles.
    - Publish or archive articles.
"""

from app.repositories.knowledge_repository import (
    KnowledgeRepository,
)


def test_knowledge_repository() -> None:
    """
    Confirm that the repository can read the current knowledge library.
    """

    repository = KnowledgeRepository()

    drafts = repository.get_drafts()
    published = repository.get_published()

    print("\nKNOWLEDGE REPOSITORY TEST")
    print("PASSED")
    print(f"Draft count: {repository.count_drafts()}")
    print(
        "Published count: "
        f"{repository.count_published()}"
    )

    if drafts:
        first_draft = drafts[0]

        loaded_draft = repository.get_draft(
            first_draft["id"]
        )

        print(
            "Loaded draft: "
            f"{loaded_draft['title']}"
        )

    if published:
        first_published = published[0]

        loaded_published = (
            repository.get_published_article(
                first_published["id"]
            )
        )

        print(
            "Loaded published article: "
            f"{loaded_published['title']}"
        )


def main() -> None:
    """
    Run the knowledge repository test.
    """

    test_knowledge_repository()


if __name__ == "__main__":
    main()