from copy import deepcopy
from datetime import datetime

from app.models.published_article import PublishedArticle


class PublicationService:
    """
    Convert validated drafts into published articles.
    """

    def publish(self, draft):
        """
        Publish a validated draft.
        """

        metadata = deepcopy(draft["metadata"])

        metadata.status = "Published"
        metadata.version = "1.0"
        metadata.published_at = datetime.now().strftime(
            "%b %d, %Y %I:%M %p"
        )

        article = PublishedArticle(
            command_name=draft["command_name"],
            description=draft["description"],
            summary=draft["summary"],
            syntax=draft["syntax"],
            examples=deepcopy(draft["examples"]),
            important_fields=deepcopy(
                draft["important_fields"]
            ),
            common_errors=deepcopy(
                draft["common_errors"]
            ),
            related_commands=deepcopy(
                draft["related_commands"]
            ),
            official_references=deepcopy(
                draft["official_references"]
            ),
            explanation=deepcopy(
                draft["explanation"]
            ),
            metadata=metadata,
        )

        return article