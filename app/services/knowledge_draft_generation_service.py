from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.article_identity_resolver import ArticleIdentityResolver
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)
from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchError,
    KnowledgeSourceResearchService,
)
from app.services.knowledge_evidence_extraction_service import (
    KnowledgeEvidenceExtractionService,
)


GENERATION_STATUSES = (
    "proposed", "preparing", "draft", "needs_evidence", "needs_revision",
    "refinement_in_progress", "ready_for_review", "accepted_into_content_studio",
    "rejected", "superseded",
)


class KnowledgeDraftGenerationError(ValueError):
    pass


class KnowledgeDraftGenerationService:
    """Human-initiated, evidence-grounded article draft preparation."""

    def __init__(self, repository_root: Path | None = None,
                 campaign_root: Path | None = None,
                 taxonomy_path: Path | None = None,
                 policy_path: Path | None = None,
                 repository: KnowledgeRepository | None = None):
        self.repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.campaign_root = (campaign_root or self.repository_root / "knowledge_campaigns").resolve()
        self.package_root = self.campaign_root / "draft_generation"
        self.planner = KnowledgeCoveragePlannerService(
            self.repository_root, self.campaign_root,
            taxonomy_path or self.repository_root / "app" / "data" / "knowledge_coverage_taxonomy.json",
        )
        self.research = KnowledgeSourceResearchService(
            self.repository_root, self.campaign_root,
            policy_path=policy_path,
            taxonomy_path=taxonomy_path,
        )
        self.repository = repository or KnowledgeRepository(self.repository_root / "knowledge_base")
        self.identity = ArticleIdentityResolver(self.repository)
        self.extraction = KnowledgeEvidenceExtractionService(
            self.repository_root, self.campaign_root, policy_path=policy_path,
            taxonomy_path=taxonomy_path,
        )

    def list_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        if not self.package_root.exists():
            return []
        packages = [self._read(path) for path in self.package_root.glob("KDG-*.json")]
        return sorted((item for item in packages if item["campaign_id"] == campaign_id),
                      key=lambda item: item["created_at"], reverse=True)

    def get(self, package_id: str) -> dict[str, Any]:
        path = self._path(package_id)
        if not path.exists():
            raise KnowledgeDraftGenerationError(f"Draft-generation package '{package_id}' was not found.")
        return self._read(path)

    def prepare(self, campaign_id: str, gap_id: str, work_item_id: str,
                notes: str = "") -> dict[str, Any]:
        campaign, gap, work = self._context(campaign_id, gap_id, work_item_id)
        package_id = self._stable_id("KDG", campaign_id, gap_id, work_item_id, "knowledge_article")
        path = self._path(package_id)
        if path.exists():
            return self._read(path)

        area = self._area(campaign, gap["area_id"])
        platform = str((campaign.get("platforms") or [""])[0])
        title = f"{area['title']} Troubleshooting Guide"
        article_id = self._slug(f"{platform} {area['title']} troubleshooting guide")
        now = self._now()
        considered = [asset for asset in campaign.get("existing_assets", [])
                      if gap["area_id"] in asset.get("areas", [])]
        package = {
            "schema_version": "1.0", "package_id": package_id,
            "campaign_id": campaign_id, "gap_id": gap_id, "work_item_id": work_item_id,
            "research_package_ids": [], "requested_asset_type": "knowledge_article",
            "canonical_identity": article_id, "proposed_title": title,
            "proposed_purpose": campaign["objective"], "platform": platform,
            "domain": campaign["domain"], "coverage_area": gap["area_id"],
            "existing_assets_considered": considered, "reused_assets": [],
            "approved_sources_used": [], "source_provenance": [],
            "evidence_snapshot": {}, "generation_status": "preparing",
            "validation_results": [], "confidence": "not_assessed", "warnings": [],
            "human_notes": str(notes or "").strip(), "draft_preview": None,
            "content_studio_article_id": None, "created_at": now, "updated_at": now,
            "history": [{"event": "preparation_requested", "at": now, "actor": "Human"}],
        }
        self._evaluate(package, campaign, gap, area)
        self._save(package)
        self._attach_reference(campaign, package)
        return deepcopy(package)

    def accept_into_content_studio(self, package_id: str) -> dict[str, Any]:
        package = self.get(package_id)
        if package["generation_status"] == "accepted_into_content_studio":
            return package
        if package["generation_status"] != "ready_for_review" or not package.get("draft_preview"):
            raise KnowledgeDraftGenerationError(
                "Only an evidence-complete, validated draft can be accepted into Content Studio."
            )
        article = deepcopy(package["draft_preview"])
        match = self.identity.resolve(article["id"], article, include_drafts=True)
        if match:
            package["generation_status"] = "superseded"
            package["reused_assets"] = [self._match_record(match)]
            package["warnings"] = [f"Reuse existing article '{match.article['id']}' instead of creating a duplicate."]
            package["history"].append({"event": "handoff_blocked_by_existing_identity",
                                       "at": self._now(), "actor": "Identity Service",
                                       "article_id": match.article["id"]})
            package["updated_at"] = self._now()
            self._save(package); self._sync_reference(package)
            return deepcopy(package)
        self.repository.save_draft(article)
        package["generation_status"] = "accepted_into_content_studio"
        package["content_studio_article_id"] = article["id"]
        package["updated_at"] = self._now()
        package["history"].append({"event": "accepted_into_content_studio", "at": package["updated_at"],
                                   "actor": "Human", "article_id": article["id"]})
        self._save(package); self._sync_reference(package)
        return deepcopy(package)

    def reject(self, package_id: str, notes: str = "") -> dict[str, Any]:
        package = self.get(package_id)
        package["generation_status"] = "rejected"
        package["human_notes"] = str(notes or "").strip()
        package["updated_at"] = self._now()
        package["history"].append({"event": "rejected", "at": package["updated_at"], "actor": "Human"})
        self._save(package); self._sync_reference(package)
        return deepcopy(package)

    def refresh_from_approved_claim_plan(self, package_id: str) -> dict[str, Any]:
        """Human-initiated regeneration; only reviewed claim-plan claims are consumed."""
        package = self.get(package_id)
        if package.get("generation_status") in {"rejected", "superseded", "accepted_into_content_studio"}:
            raise KnowledgeDraftGenerationError("This package lifecycle no longer permits regeneration.")
        campaign, gap, _ = self._context(package["campaign_id"], package["gap_id"],
                                         package["work_item_id"])
        self._evaluate(package, campaign, gap, self._area(campaign, gap["area_id"]))
        self._save(package)
        self._sync_reference(package)
        return deepcopy(package)

    def _evaluate(self, package: dict[str, Any], campaign: dict[str, Any],
                  gap: dict[str, Any], area: dict[str, Any]) -> None:
        identity_probe = {"id": package["canonical_identity"], "canonical_id": package["canonical_identity"],
                          "title": package["proposed_title"], "category": campaign["category"],
                          "overview": package["proposed_purpose"]}
        match = self.identity.resolve(package["canonical_identity"], identity_probe, include_drafts=True)
        if match:
            package["generation_status"] = "superseded"
            package["reused_assets"] = [self._match_record(match)]
            package["warnings"] = [f"A canonical or equivalent article already exists: {match.article['id']}."]
            package["confidence"] = "high"
            package["history"].append({"event": "reuse_recommended", "at": self._now(),
                                       "actor": "Identity Service", "article_id": match.article["id"]})
            package["updated_at"] = self._now()
            return

        approved = [item for item in self.research.list_for_campaign(campaign["campaign_id"])
                    if item.get("status") == "approved" and
                    item.get("target_coverage_area") == gap["area_id"]]
        sources, provenance = self._approved_sources(approved)
        structured_evidence = self.extraction.approved_units_for(
            [item["package_id"] for item in approved]
        )
        package["research_package_ids"] = [item["package_id"] for item in approved]
        package["approved_sources_used"] = sources
        package["source_provenance"] = provenance
        package["evidence_snapshot"] = {
            "campaign_fingerprint": campaign.get("coverage_snapshot", {}).get("fingerprint"),
            "gap_evidence": list(gap.get("evidence") or []),
            "research_package_digests": [self._fingerprint(item) for item in approved],
            "approved_structured_evidence": structured_evidence,
            "approved_structured_evidence_ids": [item["evidence_id"] for item in structured_evidence],
        }
        if not sources:
            package["generation_status"] = "needs_evidence"
            package["confidence"] = "low"
            package["warnings"] = [
                "No human-approved authoritative source package supports this coverage area.",
                "Approve relevant source evidence before preparing article content.",
            ]
            package["history"].append({"event": "needs_evidence", "at": self._now(),
                                       "actor": "Draft Composer"})
            package["updated_at"] = self._now()
            return

        planned_claims = self._approved_planned_claims(package["package_id"])
        package["evidence_snapshot"]["approved_claim_plan_claims"] = planned_claims
        package["evidence_snapshot"]["approved_claim_ids"] = [item["claim_id"] for item in planned_claims]
        if package.get("claim_plan_id") and not planned_claims:
            package["generation_status"] = "needs_evidence"
            package["confidence"] = "low"
            package["draft_preview"] = None
            package["warnings"] = ["The supervised claim plan has no approved, current claims available for drafting."]
            package["updated_at"] = self._now()
            return
        article = self._compose(package, campaign, area, sources, structured_evidence, planned_claims)
        errors = ArticleValidator.validate(article)
        package["validation_results"] = ([{"level": "error", "message": error} for error in errors] or
                                         [{"level": "passed", "message": "Article schema validation passed."},
                                          {"level": "passed", "message": "Every source came from an approved research package."},
                                          {"level": "passed", "message": "Canonical identity has no known conflict."}])
        package["draft_preview"] = article
        package["generation_status"] = "needs_revision" if errors else "ready_for_review"
        package["confidence"] = "medium" if not errors else "low"
        package["warnings"] = (["Resolve deterministic validation errors before review."] if errors else
                               ["This evidence-grounded draft still requires human technical and editorial review."])
        package["updated_at"] = self._now()
        package["history"].append({"event": package["generation_status"], "at": package["updated_at"],
                                   "actor": "Deterministic Draft Composer"})

    def _compose(self, package: dict[str, Any], campaign: dict[str, Any],
                 area: dict[str, Any], sources: list[dict[str, Any]],
                 structured_evidence: list[dict[str, Any]],
                 planned_claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        article = create_article_template()
        planned_claims = planned_claims or []
        planned_by_section: dict[str, list[str]] = {}
        for claim in planned_claims:
            planned_by_section.setdefault(claim.get("section", "procedure"), []).append(
                claim.get("normalized_claim", "")
            )
        source_titles = [item["title"] for item in sources]
        article.update({
            "id": package["canonical_identity"], "canonical_id": package["canonical_identity"],
            "title": package["proposed_title"], "category": campaign["category"],
            "difficulty": "Beginner", "estimated_time": "Review required",
            "overview": (f"A supervised {area['title']} draft for {package['platform']}, assembled only "
                         "from human-approved authoritative evidence. Technical guidance must be reviewed "
                         "in Content Studio before publication."),
            "tags": sorted({campaign["category"].casefold(), package["platform"].casefold(),
                            *[term.casefold() for term in area.get("terms", [])[:5]]}),
            "checklist": (planned_by_section.get("procedure") or
                          [item["normalized_claim"] for item in structured_evidence
                           if item.get("evidence_type") == "procedure"] or
                          [f"Review the approved guidance in {title}." for title in source_titles]),
            "common_indicators": (planned_by_section.get("symptoms") or
                                  [item["normalized_claim"] for item in structured_evidence
                                   if item.get("evidence_type") == "symptoms"] or
                                  [f"Coverage gap identified for {area['title']}."]),
            "commands": [], "related_topics": [area["title"], campaign["scope"]], "quiz": [],
            "sources": sources,
            "generation": {"provider": "Gnojo Knowledge Factory",
                           "model": "deterministic-evidence-composer-v1",
                           "generated_at": self._now()},
            "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None,
                       "notes": ["Generated from approved campaign evidence; human technical review required."]},
            "knowledge_factory": {
                "generation_package_id": package["package_id"], "campaign_id": package["campaign_id"],
                "gap_id": package["gap_id"], "work_item_id": package["work_item_id"],
                "research_package_ids": list(package["research_package_ids"]),
                "source_candidate_ids": [item["source_candidate_id"] for item in package["source_provenance"]],
                "evidence_ids": [item["evidence_id"] for item in structured_evidence],
                "claim_plan_id": package.get("claim_plan_id"),
                "claim_ids": [item["claim_id"] for item in planned_claims],
            },
        })
        return article

    def _approved_planned_claims(self, package_id: str) -> list[dict[str, Any]]:
        # Local import avoids making claim planning a constructor dependency of Phase 3.
        from app.services.knowledge_claim_planning_service import KnowledgeClaimPlanningService
        return KnowledgeClaimPlanningService(self, self.campaign_root).approved_claims_for(package_id)

    @staticmethod
    def _approved_sources(packages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sources, provenance, seen = [], [], set()
        for package in packages:
            selected = set(package.get("selected_sources") or [])
            for candidate in package.get("candidate_sources") or []:
                if candidate.get("source_candidate_id") not in selected or candidate.get("review_state") != "selected":
                    continue
                url = candidate.get("canonical_url")
                if not url or url in seen or candidate.get("topic_relevant") is not True:
                    continue
                seen.add(url)
                sources.append({"title": candidate.get("page_title") or "Authoritative source", "url": url})
                provenance.append({
                    "research_package_id": package["package_id"],
                    "source_candidate_id": candidate["source_candidate_id"],
                    "canonical_url": url, "authority_tier": candidate.get("authority_tier"),
                    "content_digest": candidate.get("provenance", {}).get("content_digest"),
                    "approved_package_status": package["status"], "candidate_review_state": "selected",
                })
        return sources, provenance

    def _context(self, campaign_id: str, gap_id: str, work_item_id: str):
        try:
            campaign = self.planner.get(campaign_id)
        except KnowledgeCoveragePlannerError as error:
            raise KnowledgeDraftGenerationError(str(error)) from error
        gap = next((item for item in campaign.get("gaps", []) if item.get("gap_id") == gap_id), None)
        work = next((item for item in campaign.get("work_items", [])
                     if item.get("work_item_id") == work_item_id and item.get("gap_id") == gap_id), None)
        if gap is None or work is None:
            raise KnowledgeDraftGenerationError("Draft preparation must reference a current campaign gap and work item.")
        if campaign.get("status") == "draft" or not campaign.get("last_analyzed_at"):
            raise KnowledgeDraftGenerationError("Analyze the campaign before preparing drafts.")
        if gap.get("gap_type") != "missing_article" or work.get("work_type") != "knowledge_article":
            raise KnowledgeDraftGenerationError("Phase 3 currently supports knowledge-article work items only.")
        if work.get("status") != "proposed":
            raise KnowledgeDraftGenerationError("Only proposed work items are eligible for draft preparation.")
        if work.get("dependencies"):
            raise KnowledgeDraftGenerationError("Resolve blocking work-item dependencies before preparing a draft.")
        return campaign, gap, work

    def _area(self, campaign: dict[str, Any], area_id: str) -> dict[str, Any]:
        domain = next((item for item in self.planner.domains() if item["id"] == campaign["domain"]), None)
        area = next((item for item in (domain or {}).get("areas", []) if item["id"] == area_id), None)
        if not area:
            raise KnowledgeDraftGenerationError("Campaign coverage area is no longer defined.")
        return area

    @staticmethod
    def _match_record(match) -> dict[str, Any]:
        return {"content_type": "article", "identifier": match.article["id"],
                "title": match.article.get("title"), "match_method": match.method,
                "confidence": match.confidence, "reasoning": match.reasoning}

    def _attach_reference(self, campaign: dict[str, Any], package: dict[str, Any]) -> None:
        refs = campaign.setdefault("draft_generation_packages", [])
        if not any(item["package_id"] == package["package_id"] for item in refs):
            refs.append(self._reference(package))
            campaign.setdefault("history", []).append({"event": "draft_generation_prepared",
                "at": self._now(), "actor": "Human", "package_id": package["package_id"]})
            self.planner._save(campaign)

    def _sync_reference(self, package: dict[str, Any]) -> None:
        campaign = self.planner.get(package["campaign_id"])
        refs = campaign.setdefault("draft_generation_packages", [])
        replacement = self._reference(package)
        for index, item in enumerate(refs):
            if item["package_id"] == package["package_id"]:
                refs[index] = replacement; break
        else:
            refs.append(replacement)
        self.planner._save(campaign)

    @staticmethod
    def _reference(package: dict[str, Any]) -> dict[str, Any]:
        return {"package_id": package["package_id"], "gap_id": package["gap_id"],
                "work_item_id": package["work_item_id"], "canonical_identity": package["canonical_identity"],
                "status": package["generation_status"], "updated_at": package["updated_at"]}

    def _path(self, package_id: str) -> Path:
        if not re.fullmatch(r"KDG-[A-F0-9]{12}", str(package_id or "")):
            raise KnowledgeDraftGenerationError("Invalid draft-generation package ID.")
        return self.package_root / f"{package_id}.json"

    def _save(self, package: dict[str, Any]) -> None:
        self.package_root.mkdir(parents=True, exist_ok=True)
        path = self._path(package["package_id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(package, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeDraftGenerationError(f"Unable to read draft-generation package: {error}") from error

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.casefold())).strip("-")

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
