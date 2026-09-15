"""Promote one reviewed source article into the active persistent repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data_root import APPLICATION_ROOT, resolve_data_root
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import (
    ArticleAlreadyExistsError,
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.article_review_service import ArticleReviewService
from app.services.knowledge_identity_service import (
    KnowledgeIdentityError,
    KnowledgeIdentityService,
)
from app.services.knowledge_integrity_service import KnowledgeIntegrityService


class PublishedArticlePromotionError(RuntimeError):
    """Raised when a tracked article cannot be promoted safely."""


class _ReadOnlyPublishedArticles:
    """The identity resolver's minimal read-only repository contract."""

    def __init__(self, knowledge_base_directory: Path, articles: list[dict[str, Any]]):
        self.knowledge_base_directory = knowledge_base_directory
        self._articles = articles

    def get_published(self) -> list[dict[str, Any]]:
        return list(self._articles)


class PublishedArticlePromotionService:
    """Validate and promote exactly one immutable, already-reviewed article."""

    def __init__(
        self,
        *,
        source_root: str | Path | None = None,
        data_root: str | Path | None = None,
    ) -> None:
        self.source_root = Path(source_root or APPLICATION_ROOT).expanduser().resolve()
        if data_root is None:
            configured = str(os.getenv("GNOJO_DATA_ROOT") or "").strip()
            if not configured:
                raise PublishedArticlePromotionError(
                    "GNOJO_DATA_ROOT must be set for published article promotion."
                )
            data_root = resolve_data_root()
        self.data_root = Path(data_root).expanduser().resolve()
        if self.data_root == self.source_root or self.source_root in self.data_root.parents:
            raise PublishedArticlePromotionError(
                "The promotion target must be outside the application source tree."
            )
        self.source_directory = self.source_root / "knowledge_base" / "published"
        self.knowledge_directory = self.data_root / "knowledge_base"
        self.target_directory = self.knowledge_directory / "published"
        self.inventory_path = self.knowledge_directory / "inventory.json"
        for path in (self.knowledge_directory, self.target_directory, self.inventory_path):
            try:
                path.resolve().relative_to(self.data_root)
            except ValueError as error:
                raise PublishedArticlePromotionError(
                    "The promotion target escapes GNOJO_DATA_ROOT."
                ) from error

    def preview(self, article_id: str) -> dict[str, Any]:
        """Return a write-free promotion assessment."""
        return self._assessment(article_id)

    def apply(self, article_id: str) -> dict[str, Any]:
        """Promote one safe artifact and roll back every partial write on failure."""
        assessment = self._assessment(article_id)
        if assessment["result"] != "SAFE_TO_PROMOTE":
            return assessment

        self._require_initialized_target()
        initial_hash = assessment["source_sha256"]
        inventory_existed = self.inventory_path.exists()
        inventory_before = (
            self.inventory_path.read_bytes() if inventory_existed else None
        )
        target_path = Path(assessment["target_path"])
        before_count = int(assessment["current_published_article_count"])
        article_written = False
        rollback_result = "NOT_REQUIRED"

        try:
            source_bytes = self._read_source_bytes(Path(assessment["source_path"]))
            current_hash = hashlib.sha256(source_bytes).hexdigest()
            if current_hash != initial_hash:
                raise PublishedArticlePromotionError(
                    "The tracked source article changed after validation; promotion refused."
                )

            repeated = self._assessment(article_id, source_bytes=source_bytes)
            if repeated["result"] != "SAFE_TO_PROMOTE":
                raise PublishedArticlePromotionError(
                    "; ".join(repeated["blocking_reasons"])
                )

            final_source_bytes = self._read_source_bytes(Path(assessment["source_path"]))
            if hashlib.sha256(final_source_bytes).hexdigest() != initial_hash:
                raise PublishedArticlePromotionError(
                    "The tracked source article changed immediately before mutation; promotion refused."
                )
            article = json.loads(final_source_bytes.decode("utf-8"))
            repository = KnowledgeRepository(self.knowledge_directory)
            repository.save_published(article, overwrite=False)
            article_written = True

            KnowledgeIntegrityService(self.data_root).rebuild_index()
            resolved = repository.get_published_article(article_id)
            if KnowledgeIdentityService.canonical_id(resolved) != article_id:
                raise PublishedArticlePromotionError(
                    "The promoted article could not be resolved by its canonical identity."
                )
            after_count = repository.count_published()
            if after_count != before_count + 1:
                raise PublishedArticlePromotionError(
                    "The production published article count did not increase by exactly one."
                )
            self._verify_inventory(repository)
        except Exception as error:
            rollback_result = self._rollback(
                target_path,
                article_written=article_written,
                inventory_existed=inventory_existed,
                inventory_before=inventory_before,
            )
            if isinstance(error, PublishedArticlePromotionError):
                message = str(error)
            elif isinstance(error, (ArticleAlreadyExistsError, KnowledgeRepositoryError)):
                message = str(error)
            else:
                message = f"Promotion failed: {error}"
            return {
                **assessment,
                "operation": "PROMOTE_PUBLISHED_ARTICLE",
                "mode": "apply",
                "result": "BLOCKED",
                "blocking_reasons": [message],
                "rollback_result": rollback_result,
                "after_published_article_count": before_count,
                "inventory_result": "ROLLED_BACK",
            }

        return {
            **assessment,
            "operation": "PROMOTE_PUBLISHED_ARTICLE",
            "mode": "apply",
            "result": "PROMOTED",
            "after_published_article_count": after_count,
            "inventory_result": "REBUILT_AND_VERIFIED",
            "rollback_result": rollback_result,
        }

    def _assessment(
        self,
        article_id: str,
        *,
        source_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        blocking: list[str] = []
        article_id = str(article_id or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article_id):
            raise PublishedArticlePromotionError(
                "Article must be a canonical lowercase slug."
            )
        source_path = self.source_directory / f"{article_id}.json"
        target_path = self.target_directory / f"{article_id}.json"
        try:
            source_path.resolve().relative_to(self.source_directory.resolve())
            target_path.resolve().relative_to(self.target_directory.resolve())
        except ValueError as error:
            raise PublishedArticlePromotionError(
                "The requested article path escapes its governed repository."
            ) from error
        if source_path.name != f"{article_id}.json":
            blocking.append("The source filename does not match the requested article.")

        try:
            source_bytes = source_bytes or self._read_source_bytes(source_path)
            article = json.loads(source_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PublishedArticlePromotionError(
                f"The tracked source article cannot be read safely: {error}"
            ) from error
        if not isinstance(article, dict):
            raise PublishedArticlePromotionError(
                "The tracked source article must contain one JSON object."
            )

        source_hash = hashlib.sha256(source_bytes).hexdigest()
        self._validate_article(article_id, article, blocking)
        blocking.extend(self._target_initialization_errors())
        target_articles, target_errors = self._read_target_articles()
        blocking.extend(target_errors)
        exact_target_exists = target_path.is_file()
        if exact_target_exists:
            blocking.append("The exact target article already exists.")

        canonical_collision = False
        collision_identity = ""
        try:
            if not exact_target_exists:
                aliases_path = self.knowledge_directory / "aliases.json"
                aliases = {}
                if aliases_path.exists():
                    alias_document = json.loads(aliases_path.read_text(encoding="utf-8"))
                    if not isinstance(alias_document, dict) or not isinstance(
                        alias_document.get("aliases", {}), dict
                    ):
                        raise ValueError("knowledge aliases must contain an aliases object")
                    aliases = alias_document.get("aliases", {})
                alias_target = str(aliases.get(article_id) or "").strip()
                if alias_target:
                    canonical_collision = True
                    collision_identity = alias_target
                    blocking.append(
                        f"The requested identity is already an alias for '{alias_target}'."
                    )
                match = ArticleIdentityResolver(
                    _ReadOnlyPublishedArticles(self.knowledge_directory, target_articles)
                ).resolve_published(candidate=article)
                if match and not canonical_collision:
                    canonical_collision = True
                    collision_identity = str(match.article.get("id") or "")
                    blocking.append(
                        "A canonical-equivalent published article already exists"
                        + (f" as '{collision_identity}'." if collision_identity else ".")
                    )
        except (KnowledgeIdentityError, OSError, json.JSONDecodeError) as error:
            blocking.append(f"Canonical collision validation failed: {error}")

        review = article.get("review") if isinstance(article.get("review"), dict) else {}
        current_count = len(list(self.target_directory.glob("*.json"))) if self.target_directory.is_dir() else 0
        return {
            "operation": "PROMOTE_PUBLISHED_ARTICLE",
            "mode": "dry_run",
            "source_path": str(source_path),
            "target_path": str(target_path),
            "title": str(article.get("title") or ""),
            "id": str(article.get("id") or ""),
            "canonical_id": str(article.get("canonical_id") or ""),
            "version": article.get("version"),
            "reviewer": str(review.get("reviewed_by") or ""),
            "reviewed_at": str(review.get("reviewed_at") or ""),
            "published_at": str(article.get("published_at") or ""),
            "source_sha256": source_hash,
            "exact_target_exists": exact_target_exists,
            "canonical_collision_exists": canonical_collision,
            "canonical_collision_identity": collision_identity,
            "current_published_article_count": current_count,
            "projected_published_article_count": current_count + (0 if blocking else 1),
            "inventory_rebuild_required": not blocking,
            "blocking_reasons": blocking,
            "result": "BLOCKED" if blocking else "SAFE_TO_PROMOTE",
        }

    @staticmethod
    def _validate_article(
        article_id: str,
        article: dict[str, Any],
        blocking: list[str],
    ) -> None:
        if article.get("id") != article_id:
            blocking.append("The article ID does not match the requested source filename.")
        if article.get("canonical_id") != article_id:
            blocking.append("The canonical article ID does not match the requested source filename.")
        try:
            normalized = KnowledgeIdentityService.normalize(article)
            if normalized.get("id") != article_id:
                blocking.append("The normalized canonical identity does not match the request.")
        except KnowledgeIdentityError as error:
            blocking.append(str(error))

        validation_errors = ArticleValidator.validate(article)
        blocking.extend(f"Article schema: {error}" for error in validation_errors)
        review = article.get("review")
        if not isinstance(review, dict):
            blocking.append("Approved review metadata is required.")
            return
        if review.get("status") != "approved":
            blocking.append("The tracked article is not approved.")
        if not str(review.get("reviewed_by") or "").strip():
            blocking.append("Reviewer identity is required.")
        if not PublishedArticlePromotionService._valid_timestamp(review.get("reviewed_at")):
            blocking.append("A valid review timestamp is required.")
        checks = review.get("checks")
        if not isinstance(checks, dict) or not all(
            checks.get(key) is True for key in ArticleReviewService.CHECKS
        ):
            blocking.append("Every required review check must be complete.")
        if not PublishedArticlePromotionService._valid_timestamp(article.get("published_at")):
            blocking.append("A valid publication timestamp is required.")
        version = article.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            blocking.append("A positive integer article version is required.")
        history = article.get("version_history")
        if not isinstance(history, list) or not history:
            blocking.append("Valid version history is required.")
        elif not any(
            isinstance(item, dict)
            and item.get("version") == version
            and PublishedArticlePromotionService._valid_timestamp(item.get("published_at"))
            and str(item.get("reviewed_by") or "").strip()
            for item in history
        ):
            blocking.append("Version history does not contain the current reviewed version.")

    def _read_target_articles(self) -> tuple[list[dict[str, Any]], list[str]]:
        if not self.target_directory.exists():
            return [], []
        if not self.target_directory.is_dir():
            return [], ["The production published-article path is not a directory."]
        articles: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in sorted(self.target_directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("not a JSON object")
                KnowledgeIdentityService.canonical_id(value)
                articles.append(value)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, KnowledgeIdentityError) as error:
                errors.append(f"Existing target article '{path.name}' cannot be validated: {error}")
        return articles, errors

    def _require_initialized_target(self) -> None:
        errors = self._target_initialization_errors()
        if errors:
            raise PublishedArticlePromotionError(errors[0])

    def _target_initialization_errors(self) -> list[str]:
        required = (
            self.knowledge_directory,
            self.target_directory,
            self.knowledge_directory / "drafts",
            self.knowledge_directory / "archive",
            self.knowledge_directory / "deleted",
        )
        if not all(path.is_dir() for path in required):
            return [
                "The active GNOJO_DATA_ROOT knowledge repository is not initialized."
            ]
        return []

    def _verify_inventory(self, repository: KnowledgeRepository) -> None:
        try:
            inventory = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PublishedArticlePromotionError(
                f"The rebuilt production inventory cannot be read: {error}"
            ) from error
        indexed = {
            str(item.get("id"))
            for item in (inventory.get("articles") or [])
            if isinstance(item, dict) and item.get("id")
        }
        published = {
            KnowledgeIdentityService.canonical_id(item)
            for item in repository.get_published()
        }
        if indexed != published:
            raise PublishedArticlePromotionError(
                "The rebuilt production inventory does not match published repository truth."
            )

    def _rollback(
        self,
        target_path: Path,
        *,
        article_written: bool,
        inventory_existed: bool,
        inventory_before: bytes | None,
    ) -> str:
        try:
            if article_written:
                target_path.unlink(missing_ok=True)
            if inventory_existed:
                self._write_bytes_atomic(self.inventory_path, inventory_before or b"")
            else:
                self.inventory_path.unlink(missing_ok=True)
            if target_path.exists():
                raise OSError("the promoted article still exists")
            if inventory_existed and self.inventory_path.read_bytes() != inventory_before:
                raise OSError("the production inventory was not restored")
            if not inventory_existed and self.inventory_path.exists():
                raise OSError("the newly created production inventory still exists")
        except OSError as error:
            return f"FAILED: {error}"
        return "VERIFIED"

    @staticmethod
    def _write_bytes_atomic(path: Path, content: bytes) -> None:
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False, suffix=".tmp") as file:
                temporary_name = file.name
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _read_source_bytes(path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
