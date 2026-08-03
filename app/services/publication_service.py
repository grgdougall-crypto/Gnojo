from copy import deepcopy
from datetime import datetime

from app.models.published_article import PublishedArticle
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.publication_mapper import PublicationMapper


class PublicationService:
    """
    Convert validated drafts into published articles
    and save them to the knowledge repository.
    """

    def __init__(self):
        self.repository = KnowledgeRepository()
        self.mapper = PublicationMapper()

    def publish(
        self,
        draft,
        category="Networking",
    ):
        """
        Publish a validated draft.
        """

        metadata = deepcopy(
            draft["metadata"]
        )

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
            examples=deepcopy(
                draft["examples"]
            ),
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

        repository_article = (
            self.mapper.to_repository_article(
                article,
                category=category,
            )
        )

        self.repository.save_published(
            repository_article,
            overwrite=True,
        )

        return article