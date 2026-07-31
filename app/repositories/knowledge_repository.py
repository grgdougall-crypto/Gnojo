"""
Purpose:
    Provide file-based access to SupportPilot knowledge articles.

Responsibilities:
    - Load draft, published, and archived articles.
    - Load one article by ID.
    - Save draft articles.
    - Move approved articles into the published library.
    - Move retired articles into the archive.
    - Hide file-system details from routes and services.

Does NOT:
    - Generate articles.
    - Validate article content.
    - Render templates.
    - Call AI providers.
"""

import json
import shutil
from pathlib import Path
from typing import Any


class KnowledgeRepositoryError(Exception):
    """
    Base exception for knowledge repository failures.
    """


class ArticleNotFoundError(KnowledgeRepositoryError):
    """
    Raised when a requested article cannot be found.
    """


class ArticleAlreadyExistsError(KnowledgeRepositoryError):
    """
    Raised when an operation would overwrite an existing article.
    """


class KnowledgeRepository:
    """
    Manage SupportPilot knowledge articles stored as JSON files.
    """

    def __init__(
        self,
        knowledge_base_directory: Path | None = None,
    ) -> None:
        """
        Initialize the repository directories.

        Args:
            knowledge_base_directory:
                Optional custom knowledge base directory. When omitted,
                the repository uses the project's knowledge_base folder.
        """

        if knowledge_base_directory is None:
            project_root = Path(__file__).resolve().parents[2]

            knowledge_base_directory = (
                project_root
                / "knowledge_base"
            )

        self.knowledge_base_directory = knowledge_base_directory

        self.draft_directory = (
            self.knowledge_base_directory
            / "drafts"
        )

        self.published_directory = (
            self.knowledge_base_directory
            / "published"
        )

        self.archive_directory = (
            self.knowledge_base_directory
            / "archive"
        )

        self._ensure_directories()

    def get_drafts(self) -> list[dict[str, Any]]:
        """
        Return all draft articles.

        Returns:
            list:
                Draft articles sorted by title.
        """

        return self._load_articles_from_directory(
            self.draft_directory
        )

    def get_published(self) -> list[dict[str, Any]]:
        """
        Return all published articles.

        Returns:
            list:
                Published articles sorted by title.
        """

        return self._load_articles_from_directory(
            self.published_directory
        )

    def get_archived(self) -> list[dict[str, Any]]:
        """
        Return all archived articles.

        Returns:
            list:
                Archived articles sorted by title.
        """

        return self._load_articles_from_directory(
            self.archive_directory
        )

    def get_draft(
        self,
        article_id: str,
    ) -> dict[str, Any]:
        """
        Return one draft article by ID.
        """

        return self._load_article(
            directory=self.draft_directory,
            article_id=article_id,
        )

    def get_published_article(
        self,
        article_id: str,
    ) -> dict[str, Any]:
        """
        Return one published article by ID.
        """

        return self._load_article(
            directory=self.published_directory,
            article_id=article_id,
        )

    def save_draft(
        self,
        article: dict[str, Any],
        overwrite: bool = False,
    ) -> Path:
        """
        Save an article into the drafts directory.

        Args:
            article:
                Complete SupportPilot article.

            overwrite:
                Whether an existing draft may be replaced.

        Returns:
            Path:
                Saved article path.
        """

        return self._save_article(
            directory=self.draft_directory,
            article=article,
            overwrite=overwrite,
        )

    def publish_article(
        self,
        article_id: str,
        overwrite: bool = False,
    ) -> Path:
        """
        Move a draft article into the published directory.

        Args:
            article_id:
                Draft article ID.

            overwrite:
                Whether an existing published article may be replaced.

        Returns:
            Path:
                Published article path.
        """

        source_path = self._article_path(
            directory=self.draft_directory,
            article_id=article_id,
        )

        destination_path = self._article_path(
            directory=self.published_directory,
            article_id=article_id,
        )

        if not source_path.exists():
            raise ArticleNotFoundError(
                f"Draft article '{article_id}' was not found."
            )

        if destination_path.exists() and not overwrite:
            raise ArticleAlreadyExistsError(
                f"Published article '{article_id}' already exists."
            )

        shutil.move(
            str(source_path),
            str(destination_path),
        )

        return destination_path

    def archive_article(
        self,
        article_id: str,
        overwrite: bool = False,
    ) -> Path:
        """
        Move a published article into the archive directory.
        """

        source_path = self._article_path(
            directory=self.published_directory,
            article_id=article_id,
        )

        destination_path = self._article_path(
            directory=self.archive_directory,
            article_id=article_id,
        )

        if not source_path.exists():
            raise ArticleNotFoundError(
                f"Published article '{article_id}' was not found."
            )

        if destination_path.exists() and not overwrite:
            raise ArticleAlreadyExistsError(
                f"Archived article '{article_id}' already exists."
            )

        shutil.move(
            str(source_path),
            str(destination_path),
        )

        return destination_path

    def count_drafts(self) -> int:
        """
        Return the number of draft articles.
        """

        return self._count_articles(
            self.draft_directory
        )

    def count_published(self) -> int:
        """
        Return the number of published articles.
        """

        return self._count_articles(
            self.published_directory
        )

    def count_archived(self) -> int:
        """
        Return the number of archived articles.
        """

        return self._count_articles(
            self.archive_directory
        )

    def _load_articles_from_directory(
        self,
        directory: Path,
    ) -> list[dict[str, Any]]:
        """
        Load every valid JSON article from a directory.
        """

        articles: list[dict[str, Any]] = []

        for article_path in sorted(
            directory.glob("*.json")
        ):
            try:
                article = self._read_json_file(
                    article_path
                )

            except KnowledgeRepositoryError:
                continue

            articles.append(article)

        articles.sort(
            key=lambda article: article.get(
                "title",
                "",
            ).lower()
        )

        return articles

    def _load_article(
        self,
        directory: Path,
        article_id: str,
    ) -> dict[str, Any]:
        """
        Load one article from a specific directory.
        """

        article_path = self._article_path(
            directory=directory,
            article_id=article_id,
        )

        if not article_path.exists():
            raise ArticleNotFoundError(
                f"Article '{article_id}' was not found."
            )

        return self._read_json_file(
            article_path
        )

    def _save_article(
        self,
        directory: Path,
        article: dict[str, Any],
        overwrite: bool,
    ) -> Path:
        """
        Save one article as formatted JSON.
        """

        article_id = article.get("id")

        if not isinstance(article_id, str):
            raise KnowledgeRepositoryError(
                "Article ID must be a string."
            )

        article_id = article_id.strip()

        if not article_id:
            raise KnowledgeRepositoryError(
                "Article ID cannot be empty."
            )

        article_path = self._article_path(
            directory=directory,
            article_id=article_id,
        )

        if article_path.exists() and not overwrite:
            raise ArticleAlreadyExistsError(
                f"Article '{article_id}' already exists."
            )

        try:
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

        except OSError as error:
            raise KnowledgeRepositoryError(
                f"Unable to save article '{article_id}'."
            ) from error

        return article_path

    def _read_json_file(
        self,
        article_path: Path,
    ) -> dict[str, Any]:
        """
        Read and parse one article JSON file.
        """

        try:
            with article_path.open(
                "r",
                encoding="utf-8",
            ) as article_file:
                article = json.load(article_file)

        except OSError as error:
            raise KnowledgeRepositoryError(
                f"Unable to read '{article_path.name}'."
            ) from error

        except json.JSONDecodeError as error:
            raise KnowledgeRepositoryError(
                f"Invalid JSON in '{article_path.name}'."
            ) from error

        if not isinstance(article, dict):
            raise KnowledgeRepositoryError(
                f"Article '{article_path.name}' must contain a JSON object."
            )

        return article

    def _article_path(
        self,
        directory: Path,
        article_id: str,
    ) -> Path:
        """
        Build a safe article file path.
        """

        normalized_id = self._normalize_article_id(
            article_id
        )

        return directory / f"{normalized_id}.json"

    def _normalize_article_id(
        self,
        article_id: str,
    ) -> str:
        """
        Validate and normalize an article ID.
        """

        if not isinstance(article_id, str):
            raise KnowledgeRepositoryError(
                "Article ID must be a string."
            )

        normalized_id = article_id.strip()

        if not normalized_id:
            raise KnowledgeRepositoryError(
                "Article ID cannot be empty."
            )

        allowed_characters = set(
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789-"
        )

        if any(
            character not in allowed_characters
            for character in normalized_id
        ):
            raise KnowledgeRepositoryError(
                "Article ID may contain only lowercase letters, "
                "numbers, and hyphens."
            )

        return normalized_id

    def _count_articles(
        self,
        directory: Path,
    ) -> int:
        """
        Count JSON articles in a directory.
        """

        return len(
            list(directory.glob("*.json"))
        )

    def _ensure_directories(self) -> None:
        """
        Create required knowledge directories when missing.
        """

        for directory in [
            self.draft_directory,
            self.published_directory,
            self.archive_directory,
        ]:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )