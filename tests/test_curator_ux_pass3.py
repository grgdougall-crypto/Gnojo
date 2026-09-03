import unittest
from copy import deepcopy
from pathlib import Path

from app.services.curator_task_review_presentation_service import CuratorTaskReviewPresentationService
from app.services.curator_verification_presentation_service import CuratorVerificationPresentationService


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


class CuratorUxPassThreeTests(unittest.TestCase):
    def test_task_review_presentation_is_deterministic_complete_and_nonmutating(self):
        task = {
            "history": [{"event": f"event-{index}"} for index in range(5)],
            "original_evidence": ["one", "two"],
            "current_content": {"title": "Current"},
            "current_verification": {"status": "appears_corrected"},
        }
        original = deepcopy(task)

        result = CuratorTaskReviewPresentationService.present(task)

        self.assertEqual(result["history_count"], 5)
        self.assertEqual([item["event"] for item in result["recent_history"]], ["event-4", "event-3", "event-2"])
        self.assertEqual([item["event"] for item in result["remaining_history"]], ["event-1", "event-0"])
        self.assertEqual(result["original_evidence_count"], 2)
        self.assertTrue(result["has_current_content"])
        self.assertTrue(result["has_verification"])
        self.assertEqual(task, original)

    def test_task_detail_follows_decision_centered_order(self):
        source = (TEMPLATES / "curator_task_detail.html").read_text(encoding="utf-8")
        headings = [
            "Targeted Verification",
            "Evidence Across the Task Lifecycle",
            "Task History",
            "Review Decision",
            "Task Actions",
        ]
        positions = [source.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        for label in ["Then", "Now", "Verify", "View complete task history", "Technical Task Details", "Related Knowledge and Tasks"]:
            self.assertIn(label, source)

    def test_task_detail_preserves_actions_routes_and_assisted_resolution(self):
        source = (TEMPLATES / "curator_task_detail.html").read_text(encoding="utf-8")
        for action in ["assign", "priority", "start", "defer", "ignore", "note", "reopen", "resolve", "resolve_continue"]:
            self.assertIn(f'value="{action}"', source)
        for required in ["curator_task_verify", "curator_task_action", "curator_task_repair_preview", "_curator_resolution_package.html", "return_to", "curator_session"]:
            self.assertIn(required, source)
        self.assertIn("task.repair_eligibility is defined", source)
        self.assertIn("Structural repair preview available", source)
        self.assertIn("no repair has been applied", source)

    def test_related_content_disclosure_has_clear_native_affordance(self):
        source = (TEMPLATES / "curator_task_detail.html").read_text(encoding="utf-8")
        styles = (ROOT / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn('<details class="quality-section mt-4 curator-related-disclosure"', source)
        self.assertIn("Related Knowledge and Tasks", source)
        self.assertIn("Show related items", source)
        self.assertIn("Hide related items", source)
        self.assertIn('class="bi bi-chevron-down" aria-hidden="true"', source)
        self.assertIn(".curator-related-disclosure > summary:hover", styles)
        self.assertIn(".curator-related-disclosure > summary:focus-visible", styles)
        self.assertIn(".curator-related-disclosure[open] .curator-related-disclosure__hide", styles)
        self.assertIn("overflow-wrap: anywhere", styles)

    def test_dashboard_distinguishes_broad_curator_defects_from_knowledge_integrity(self):
        source = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Curator defects", source)
        self.assertIn("Across all Curator rule families", source)
        self.assertIn("Deterministic Knowledge Integrity", source)
        self.assertIn("distinct from the broader Curator finding count", source)

    def test_every_targeted_verification_state_retains_specialized_presentation(self):
        states = [
            "relationship_satisfied",
            "relationship_missing",
            "relationship_conflict_or_unresolved",
            "target_unavailable",
            "still_detected",
            "appears_corrected",
            "human_review_required",
        ]
        for state in states:
            with self.subTest(state=state):
                result = CuratorVerificationPresentationService.present({"status": state, "message": "detail"})
                self.assertEqual(result["technical_state"], state)
                self.assertTrue(result["headline"])
                self.assertTrue(result["explanation"])
                self.assertTrue(result["next_action"])
                self.assertEqual(result["technical_detail"], "detail")

    def test_dashboard_compresses_secondary_information_without_losing_content(self):
        source = (TEMPLATES / "curator_dashboard.html").read_text(encoding="utf-8")
        for heading in ["Secondary operational context", "Curator Status", "Lessons Learned", "Knowledge Evolution"]:
            self.assertIn(heading, source)
        self.assertGreaterEqual(source.count('<details class="curator-secondary__item">'), 3)
        self.assertIn("{% for lesson in lessons %}", source)
        self.assertIn("{% for event in dashboard.evolution %}", source)

    def test_integrity_healthy_state_is_compact_and_detailed_findings_remain_available(self):
        source = (TEMPLATES / "knowledge_integrity.html").read_text(encoding="utf-8")
        self.assertIn("Healthy checks", source)
        self.assertIn("integrity-healthy", source)
        self.assertIn("0 findings", source)
        for key in ["broken_relationships", "command_relationship_defects", "duplicate_groups", "inventory_mismatches", "missing_review_metadata", "orphaned_articles"]:
            self.assertIn(key, source)
        for action in ["knowledge_integrity_reindex", "knowledge_integrity_normalize_identities"]:
            self.assertIn(action, source)


if __name__ == "__main__":
    unittest.main()
