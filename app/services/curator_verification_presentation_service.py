class CuratorVerificationPresentationService:
    """Translate technical verification states without changing their meaning."""

    PRESENTATIONS = {
        "relationship_satisfied": ("Relationship is already correct", "The canonical relationship is present in the current content.", "No repair is needed. Review the evidence, then resolve the task if appropriate.", "success"),
        "relationship_missing": ("Relationship is still missing", "The expected canonical relationship is not present in the current content.", "Review the affected workflow and prepare the relationship repair.", "warning"),
        "relationship_conflict_or_unresolved": ("Relationship needs human review", "Curator could not resolve the current relationship to one unambiguous canonical target.", "Review the competing or unresolved relationship evidence before making a change.", "warning"),
        "target_unavailable": ("Affected content is unavailable", "The exact workflow or node could not be inspected at this time.", "Confirm that the content still exists and rerun targeted verification.", "warning"),
        "still_detected": ("The finding is still present", "Targeted verification found the same condition in the current content.", "Continue with the recommended reviewed repair or defer the task.", "warning"),
        "appears_corrected": ("The current content appears corrected", "Targeted verification no longer finds the original condition.", "Review the evidence, then resolve the task if no further work is required.", "success"),
        "human_review_required": ("A reviewer must decide the next step", "The current evidence is not sufficient for a deterministic conclusion.", "Review the technical evidence before resolving, repairing, or deferring the task.", "warning"),
    }

    @classmethod
    def present(cls, verification):
        verification = verification or {}
        technical_state = str(verification.get("status", "")).strip()
        if not technical_state:
            return {}
        headline, explanation, next_action, tone = cls.PRESENTATIONS.get(
            technical_state,
            ("Verification result requires review", "Curator returned a technical state that has no specialized presentation yet.", "Review the technical details before taking action.", "warning"),
        )
        return {
            "technical_state": technical_state,
            "headline": headline,
            "explanation": explanation,
            "next_action": next_action,
            "tone": tone,
            "technical_detail": str(verification.get("message", "")).strip(),
        }
