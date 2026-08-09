"""
Purpose:
    Provide file-based access to Gnojo knowledge articles.

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
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from app.services.knowledge_identity_service import (
    KnowledgeIdentityError,
    KnowledgeIdentityService,
)


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
    Manage Gnojo knowledge articles stored as JSON files.
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
        self.deleted_directory = self.knowledge_base_directory / "deleted"

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

    def get_deleted(self) -> list[dict[str, Any]]:
        return self._load_articles_from_directory(self.deleted_directory)

    def get_archived_article(self, article_id: str) -> dict[str, Any]:
        return self._load_article(self.archive_directory, article_id)

    def get_deleted_article(self, article_id: str) -> dict[str, Any]:
        return self._load_article(self.deleted_directory, article_id)

    def resolve_published_article(self, article_id: str) -> dict[str, Any]:
        from app.services.article_identity_resolver import ArticleIdentityResolver
        match = ArticleIdentityResolver(self).resolve(identifier=article_id)
        if not match:
            raise ArticleNotFoundError(f"Article '{article_id}' was not found.")
        return match.article

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
                Complete Gnojo article.

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

    def save_published(
        self,
        article: dict[str, Any],
        overwrite: bool = False,
    ) -> Path:
        """
        Save an article directly into the published directory.

        Args:
            article:
                Complete Gnojo article.

            overwrite:
                Whether an existing published article may be replaced.

        Returns:
            Path:
                Saved published article path.
        """

        return self._save_article(
            directory=self.published_directory,
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

        article = KnowledgeIdentityService.normalize(self._read_json_file(source_path))
        self._assert_unique_published_identity(article, destination_path)
        self._write_json_atomic(destination_path, article)
        try:
            source_path.unlink()
        except OSError as error:
            try:
                destination_path.unlink()
            except OSError:
                pass
            raise KnowledgeRepositoryError(
                f"Unable to complete publication for '{article_id}'."
            ) from error

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

    def soft_delete_article(self, article_id: str) -> Path:
        source = self._article_path(self.archive_directory, article_id)
        destination = self._article_path(self.deleted_directory, article_id)
        if not source.exists():
            raise ArticleNotFoundError(f"Archived article '{article_id}' was not found.")
        if destination.exists():
            raise ArticleAlreadyExistsError(f"Deleted article '{article_id}' already exists.")
        shutil.move(str(source), str(destination))
        return destination

    def permanently_delete_article(self, article_id: str) -> None:
        path = self._article_path(self.deleted_directory, article_id)
        if not path.exists():
            raise ArticleNotFoundError(f"Soft-deleted article '{article_id}' was not found.")
        path.unlink()

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

        try:
            article = KnowledgeIdentityService.normalize(article)
        except KnowledgeIdentityError as error:
            raise KnowledgeRepositoryError(str(error)) from error

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

        if directory == self.published_directory:
            self._assert_unique_published_identity(article, article_path)
        self._write_json_atomic(article_path, article)

        return article_path

    def find_all_by_canonical_id(self, article_id: str) -> list[tuple[str, Path, dict[str, Any]]]:
        canonical = KnowledgeIdentityService.canonical_id(article_id)
        matches = []
        for state, directory in (
            ("draft", self.draft_directory),
            ("published", self.published_directory),
            ("archived", self.archive_directory),
        ):
            for path in directory.glob("*.json"):
                try:
                    article = self._read_json_file(path)
                    if KnowledgeIdentityService.canonical_id(article) == canonical:
                        matches.append((state, path, article))
                except (KnowledgeRepositoryError, KnowledgeIdentityError):
                    continue
        return matches

    def _assert_unique_published_identity(self, article: dict[str, Any], destination: Path) -> None:
        canonical = KnowledgeIdentityService.canonical_id(article)
        for path in self.published_directory.glob("*.json"):
            if path.resolve() == destination.resolve():
                continue
            try:
                existing = self._read_json_file(path)
                existing_id = KnowledgeIdentityService.canonical_id(existing)
            except (KnowledgeRepositoryError, KnowledgeIdentityError):
                continue
            if existing_id == canonical:
                raise ArticleAlreadyExistsError(
                    f"Canonical article '{canonical}' is already published in '{path.name}'."
                )

    def _write_json_atomic(self, path: Path, article: dict[str, Any]) -> None:
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False,
                suffix=".tmp",
            ) as article_file:
                temporary_name = article_file.name
                json.dump(article, article_file, indent=2, ensure_ascii=False)
                article_file.flush()
                os.fsync(article_file.fileno())
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise KnowledgeRepositoryError(
                f"Unable to save article '{article.get('id', path.stem)}'."
            ) from error

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
            self.deleted_directory,
        ]:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )
