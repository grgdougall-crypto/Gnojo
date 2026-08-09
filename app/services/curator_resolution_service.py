from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import (
    ArticleAlreadyExistsError,
    ArticleNotFoundError,
    KnowledgeRepository,
)
from app.services.article_candidate_analyzer import ArticleCandidateAnalyzer, CREATE_NEW_ARTICLE
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.article_tag_service import ArticleTagService
from app.services.assisted_resolution_validator import AssistedResolutionValidator
from curator.memory import CuratorMemoryStore
from curator.resolution import ResolutionPackageError, ResolutionPackageRepository


class CuratorResolutionService:
    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.memory = CuratorMemoryStore(self.root / "curation_memory")
        self.packages = ResolutionPackageRepository(self.root / "curation_memory")
        self.analyzer = ArticleCandidateAnalyzer(self.root)
        self.knowledge = KnowledgeRepository(self.root / "knowledge_base")
        self.identities = ArticleIdentityResolver(self.knowledge)
        self.validator = AssistedResolutionValidator()

    def eligible_tasks(self) -> list[dict[str, Any]]:
        tasks = self.memory.load().get("tasks", {}).values()
        return sorted(
            [task for task in tasks if task.get("status") in {"open", "in_progress"} and task.get("finding_type") in self.analyzer.ELIGIBLE_TYPES],
            key=lambda task: (task.get("finding_type") != "malformed_relationship", task.get("task_id", "")),
        )

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.packages.get(task_id)

    def article_location(self, task_id: str) -> tuple[str, dict[str, Any]]:
        """Resolve a package article against durable knowledge state.

        A package retains the canonical article identity after publication, so callers
        must not assume that ``draft_article_id`` still lives in the draft directory.
        """
        package = self.get(task_id)
        if not package or not package.get("draft_article_id"):
            raise ResolutionPackageError("This package does not have an article draft yet.")
        article_id = package["draft_article_id"]
        try:
            return "draft", self.knowledge.get_draft(article_id)
        except ArticleNotFoundError:
            try:
                return "published", self.knowledge.get_published_article(article_id)
            except ArticleNotFoundError as error:
                raise ResolutionPackageError(
                    "The package article could not be found in draft or published knowledge."
                ) from error

    def prepare(self, task_id: str) -> dict[str, Any]:
        task = self.memory.load().get("tasks", {}).get(task_id)
        if not task:
            raise ResolutionPackageError("Knowledge Task was not found.")
        previous = self.get(task_id)
        retained_article_id = None
        retained_status = "prepared"
        if previous and previous.get("draft_article_id"):
            # Refreshing a package is an editorial operation, not a new identity
            # request. Keep the durable pointer when the article still exists in
            # either lifecycle state so the package can always reopen it.
            self.article_location(task_id)
            retained_article_id = previous["draft_article_id"]
            retained_status = "draft_created"
        analysis = self.analyzer.analyze(task)
        instruction = analysis["instruction"]
        facts = [instruction, *[str(item) for item in task.get("evidence", [])]]
        package = {
            "task_id": task_id, "finding_id": task.get("finding_id"), "finding_type": task.get("finding_type"),
            **analysis,
            "summary": f"Prepare reusable support content for {analysis['node_title']}.",
            "purpose": f"Help a reviewer support the {analysis['node_title']} step without changing the workflow automatically.",
            "prerequisites": ["Confirm the affected device and platform before following the guidance."],
            "steps": [instruction] if instruction else [],
            "warnings": ["Follow organization and manufacturer guidance; stop if the action exceeds your authorization."],
            "expected_result": "Record the observed result and return to the originating workflow.",
            "rollback": "No automatic rollback is proposed. Record any change and use the approved recovery path if needed.",
            "source_requirements": [{"status": "REQUIRES_SOURCE_REVIEW", "preferred_authority": "Official vendor or platform documentation", "source_type": "Primary technical documentation", "url": None}],
            "proposed_relationship": {"workflow_id": analysis["workflow_id"], "node_id": analysis["node_id"], "target_article_id": analysis["proposed_article_id"], "action": "RELINK_EXISTING" if analysis["recommendation"] != CREATE_NEW_ARTICLE else "PROPOSE_CREATE"},
            "confidence": task.get("confidence", "medium"),
            "open_questions": ["Which current authoritative source best supports this guidance?"],
            "human_decisions": ["Confirm the recommendation.", "Review technical accuracy and proportional safety guidance.", "Approve sources before publication.", "Link the article only after it is published."],
            "review_checklist": ["Workflow and node are correct", "Article ID and title are unique", "Steps match the source instruction", "Warnings are proportional", "Sources are reviewed", "Relationship remains a proposal"],
            "evidence_boundaries": {"facts_from_repository": facts, "inferences": [analysis["recommendation_reason"]], "editorial_suggestions": ["The article may support future related workflows after review."], "missing_evidence": ["An approved authoritative source has not been attached."]},
            "status": retained_status, "source_status": "requires_review",
            "draft_article_id": retained_article_id,
        }
        ids = [article.get("id") for article in self.knowledge.get_drafts() + self.knowledge.get_published()]
        package["validation_errors"] = self.validator.validate(package, ids)
        package["validation_status"] = "passed" if not package["validation_errors"] else "failed"
        return self.packages.save(package)

    def create_article_draft(self, task_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ResolutionPackageError("Human confirmation is required before creating a draft.")
        package = self.get(task_id)
        if not package or package.get("recommendation") != CREATE_NEW_ARTICLE:
            raise ResolutionPackageError("This package does not recommend creating a new article.")
        if package.get("validation_status") != "passed":
            raise ResolutionPackageError("The package must pass validation before draft creation.")
        if package.get("draft_article_id"):
            state, article = self.article_location(task_id)
            if state != "draft":
                raise ResolutionPackageError("This package article is already published.")
            return article
        # Recover a successfully persisted draft if an earlier request ended before
        # the package pointer was saved. This makes creation retry-safe without ever
        # generating a second identity.
        existing_origin = next(
            (article for article in self.knowledge.get_drafts()
             if article.get("workflow_origin", {}).get("curator_task_id") == task_id),
            None,
        )
        if existing_origin:
            return self._attach_draft(package, existing_origin, recovered=True)
        identity_match = self.identities.resolve(candidate={
            "id": package["proposed_article_id"],
            "title": package["proposed_article_title"],
            "overview": package["purpose"],
            "checklist": package["steps"],
            "workflow_references": [{
                "workflow_id": package["workflow_id"],
                "node_id": package["node_id"],
            }],
        }, include_drafts=True)
        if identity_match:
            if identity_match.article.get("workflow_origin", {}).get("curator_task_id") == task_id:
                return self._attach_draft(package, identity_match.article, recovered=True)
            raise ResolutionPackageError(
                "An equivalent knowledge record already exists. Reuse or compare it before creating a new draft: "
                f"{identity_match.article.get('canonical_id') or identity_match.article.get('id')} "
                f"({identity_match.confidence:.1%} confidence via "
                f"{identity_match.method})."
            )
        article = create_article_template()
        article.update({
            "id": package["proposed_article_id"], "title": package["proposed_article_title"],
            "category": package["category"], "difficulty": "Beginner", "estimated_time": "5 to 10 minutes",
            "overview": package["purpose"], "checklist": package["steps"],
            "common_indicators": [package["expected_result"]], "related_topics": [package["workflow_name"], package["subcategory"]],
            "quiz": [{"question": "What should you do after completing this support step?", "answers": ["Record the result and return to the workflow", "Make unrelated changes", "Ignore the result"], "correct_answer": "Record the result and return to the workflow"}],
            "sources": [],
            "generation": {"provider": "Gnojo Curator", "model": "deterministic-assisted-resolution", "generated_at": datetime.now(timezone.utc).isoformat()},
            "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None, "notes": [f"Originating Curator task: {task_id}", "Authoritative sources require human review before publication."]},
            "workflow_origin": {"filename": package["workflow_filename"], "workflow_id": package["workflow_id"], "node_id": package["node_id"], "curator_task_id": task_id},
        })
        article["tags"] = ArticleTagService.generate(article)
        errors = ArticleValidator.validate(article)
        if errors:
            raise ResolutionPackageError("Draft article failed schema validation: " + "; ".join(errors))
        try:
            self.knowledge.save_draft(article)
        except ArticleAlreadyExistsError as error:
            raise ResolutionPackageError(str(error)) from error
        return self._attach_draft(package, article)

    def _attach_draft(self, package: dict[str, Any], article: dict[str, Any], *,
                      recovered: bool = False) -> dict[str, Any]:
        package["draft_article_id"] = article["id"]
        package["status"] = "draft_created"
        self.packages.save(package)
        self.packages.record_event(
            package["task_id"],
            "article_draft_recovered" if recovered else "article_draft_created",
            article_id=article["id"], relationship_changed=False,
        )
        return article
