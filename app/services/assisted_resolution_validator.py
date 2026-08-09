from __future__ import annotations

import re
from typing import Any


class AssistedResolutionValidator:
    REQUIRED = {
        "task_id", "finding_id", "finding_type", "workflow_id", "node_id", "instruction",
        "recommendation", "recommendation_reason", "proposed_article_id", "proposed_article_title",
        "platform", "category", "subcategory", "summary", "purpose", "prerequisites", "steps",
        "warnings", "expected_result", "rollback", "source_requirements", "proposed_relationship",
        "confidence", "open_questions", "human_decisions", "review_checklist", "evidence_boundaries",
        "identity_resolution",
    }

    def validate(self, package: dict[str, Any], existing_ids=()) -> list[str]:
        errors = []
        for field in sorted(self.REQUIRED - package.keys()):
            errors.append(f"Missing package field: {field}.")
        for field in ("task_id", "finding_id", "workflow_id", "node_id", "instruction", "recommendation", "proposed_article_id", "proposed_article_title", "category", "platform"):
            if not str(package.get(field, "")).strip():
                errors.append(f"Field '{field}' is required.")
        article_id = str(package.get("proposed_article_id", ""))
        if article_id and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article_id):
            errors.append("Proposed article ID must use lowercase letters, numbers, and hyphens.")
        existing_ids = set(existing_ids)
        if package.get("recommendation") == "CREATE_NEW_ARTICLE" and article_id in existing_ids:
            errors.append("Proposed article ID already exists.")
        numbered_base = re.sub(r"-\d+$", "", article_id)
        if (package.get("recommendation") == "CREATE_NEW_ARTICLE" and numbered_base != article_id
                and numbered_base in existing_ids):
            errors.append(
                f"Numbered duplicate IDs are not allowed while canonical article '{numbered_base}' exists."
            )
        identity = package.get("identity_resolution")
        if not isinstance(identity, dict):
            errors.append("Identity resolution evidence is required.")
        elif package.get("recommendation") == "CREATE_NEW_ARTICLE" and identity.get("status") != "no_match":
            errors.append("A new article may be proposed only after identity resolution returns no match.")
        elif package.get("recommendation") == "LINK_EXISTING_ARTICLE":
            if identity.get("status") != "matched" or not identity.get("canonical_article_id"):
                errors.append("An existing-article recommendation requires a resolved canonical article.")
        for field in ("prerequisites", "steps", "warnings", "source_requirements", "open_questions", "human_decisions", "review_checklist"):
            if not isinstance(package.get(field), list):
                errors.append(f"Field '{field}' must be a list.")
        relationship = package.get("proposed_relationship")
        if not isinstance(relationship, dict) or not relationship.get("target_article_id"):
            errors.append("A proposed relationship target is required.")
        if package.get("recommendation") == "CREATE_NEW_ARTICLE" and relationship.get("target_article_id") != article_id:
            errors.append("The proposed relationship must target the proposed article ID.")
        return errors
