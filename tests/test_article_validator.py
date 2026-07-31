"""
Purpose:
    Test the SupportPilot article validator.

Responsibilities:
    - Confirm that a correctly structured article passes validation.
    - Confirm that an incorrectly structured article fails validation.
    - Display readable validation errors.

Does NOT:
    - Generate articles.
    - Save articles.
    - Modify production knowledge files.
"""

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator


def create_valid_article() -> dict:
    """
    Create a complete article that should pass validation.

    Returns:
        dict: A valid SupportPilot knowledge article.
    """

    article = create_article_template()

    article["id"] = "ethernet-connection-check"
    article["title"] = "Check an Ethernet Connection"
    article["category"] = "Networking"
    article["difficulty"] = "Beginner"
    article["estimated_time"] = "3 minutes"

    article["overview"] = (
        "An Ethernet connection uses a physical cable to connect "
        "a computer or device to a network."
    )

    article["checklist"] = [
        "Confirm that the Ethernet cable is connected.",
        "Check the network port for activity lights.",
        "Inspect the cable for visible damage.",
    ]

    article["common_indicators"] = [
        "The network icon shows no connection.",
        "The Ethernet port has no activity lights.",
        "The device cannot reach the local network.",
    ]

    article["commands"] = [
        {
            "command": "ipconfig /all",
            "description": (
                "Displays detailed Windows network configuration."
            ),
        }
    ]

    article["related_topics"] = [
        "IP address configuration",
        "Network adapter troubleshooting",
    ]

    article["quiz"] = [
        {
            "question": (
                "What should you check first when an Ethernet "
                "connection is unavailable?"
            ),
            "answers": [
                "The physical cable connection",
                "The desktop wallpaper",
                "The printer toner level",
            ],
            "correct_answer": "The physical cable connection",
        }
    ]

    article["sources"] = [
        {
            "title": "Microsoft ipconfig documentation",
            "url": (
                "https://learn.microsoft.com/windows-server/"
                "administration/windows-commands/ipconfig"
            ),
        }
    ]

    article["generation"] = {
        "provider": None,
        "model": None,
        "generated_at": None,
    }

    article["review"] = {
        "status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        "notes": [],
    }

    return article


def create_invalid_article() -> dict:
    """
    Create an incomplete article that should fail validation.

    Returns:
        dict: An invalid SupportPilot knowledge article.
    """

    article = create_article_template()

    article["id"] = ""
    article["title"] = ""
    article["category"] = "Networking"
    article["difficulty"] = "Expert"
    article["estimated_time"] = ""
    article["overview"] = ""

    article["commands"] = [
        {
            "command": "",
            "description": "",
        }
    ]

    article["quiz"] = [
        {
            "question": "",
            "answers": [
                "Answer A",
            ],
            "correct_answer": "Answer B",
        }
    ]

    article["sources"] = [
        {
            "title": "",
            "url": "",
        }
    ]

    return article


def test_valid_article() -> None:
    """
    Confirm that the valid article produces no errors.
    """

    article = create_valid_article()
    validation_errors = ArticleValidator.validate(article)

    print("\nVALID ARTICLE TEST")

    if validation_errors:
        print("FAILED")

        for validation_error in validation_errors:
            print(f"- {validation_error}")

        return

    print("PASSED")
    print("The valid article contains no validation errors.")


def test_invalid_article() -> None:
    """
    Confirm that the invalid article produces readable errors.
    """

    article = create_invalid_article()
    validation_errors = ArticleValidator.validate(article)

    print("\nINVALID ARTICLE TEST")

    if not validation_errors:
        print("FAILED")
        print("The invalid article unexpectedly passed validation.")
        return

    print("PASSED")
    print("The validator found the expected problems:")

    for validation_error in validation_errors:
        print(f"- {validation_error}")


def main() -> None:
    """
    Run all article validator tests.
    """

    test_valid_article()
    test_invalid_article()


if __name__ == "__main__":
    main()