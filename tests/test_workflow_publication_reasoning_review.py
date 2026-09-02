import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.app import app
from app.repositories.workflow_publication_review_repository import (
    WorkflowPublicationReasoningReview,
    WorkflowPublicationReviewRepository,
)
from app.services.workflow_lifecycle_projection_service import (
    NOT_READY,
    READY_FOR_PUBLICATION_REVIEW,
    WorkflowLifecycleProjectionService,
)


def workflow():
    return {
        "workflow_id": "review_demo",
        "name": "Publication Review Demo",
        "description": "Published description.",
        "start_node": "first_branch",
        "estimated_steps": 6,
        "progress_mode": "branch_aware",
        "nodes": {
            "first_branch": {
                "type": "question", "question": "Which first condition applies?",
                "answers": {
                    "a": {"label": "Condition A", "next": "inspect_a"},
                    "b": {"label": "Condition B", "next": "inspect_b"},
                },
            },
            "inspect_a": {"type": "instruction", "title": "Inspect A", "instruction": "Inspect condition A without changing it.", "next": "shared"},
            "inspect_b": {"type": "instruction", "title": "Inspect B", "instruction": "Inspect condition B without changing it.", "next": "shared"},
            "shared": {"type": "instruction", "title": "Record evidence", "instruction": "Record the evidence from the selected condition.", "next": "second_branch"},
            "second_branch": {
                "type": "question", "question": "Which second condition applies?",
                "answers": {
                    "c": {"label": "Condition C", "next": "inspect_c"},
                    "d": {"label": "Condition D", "next": "inspect_d"},
                },
            },
            "inspect_c": {"type": "instruction", "title": "Inspect C", "instruction": "Inspect condition C without changing it.", "next": "done"},
            "inspect_d": {"type": "instruction", "title": "Inspect D", "instruction": "Inspect condition D without changing it.", "next": "done"},
            "done": {"type": "resolution", "title": "Inspection complete", "message": "The bounded inspection is complete."},
        },
    }


class WorkflowPublicationReasoningReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.draft_dir = self.root / "app" / "workflow_drafts"
        self.draft_dir.mkdir(parents=True)
        published = workflow()
        self._publish(published)
        draft = deepcopy(published)
        draft["description"] = "Reviewed authored wording."
        self._write_draft(draft)
        self.repository = WorkflowPublicationReviewRepository(self.root / "curation_memory")
        self.original_config = app.config.get("WORKFLOW_REPOSITORY_ROOT")
        app.config["WORKFLOW_REPOSITORY_ROOT"] = str(self.root)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        if self.original_config is None:
            app.config.pop("WORKFLOW_REPOSITORY_ROOT", None)
        else:
            app.config["WORKFLOW_REPOSITORY_ROOT"] = self.original_config
        self.temporary.cleanup()

    def _write_draft(self, value):
        (self.draft_dir / "review_demo.json").write_text(
            json.dumps(value, indent=2) + "\n", encoding="utf-8"
        )

    def _publish(self, value):
        directory = self.root / "app" / "workflow_publications" / "review_demo"
        directory.mkdir(parents=True)
        content_hash = hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        (directory / "v0001.json").write_text(json.dumps({
            "publication": {"version": 1, "source_filename": "review_demo.json", "content_hash": content_hash},
            "workflow": value,
        }, indent=2) + "\n", encoding="utf-8")
        (directory / "current.json").write_text(json.dumps({
            "workflow_id": "review_demo", "current_version": 1, "content_hash": content_hash,
        }, indent=2) + "\n", encoding="utf-8")

    def project(self):
        return WorkflowLifecycleProjectionService(self.root).project("review_demo")

    def accept(self, finding, *, fingerprint=None, **changes):
        values = {
            "workflow_id": "review_demo",
            "draft_semantic_fingerprint": fingerprint or self.project().draft_semantic_fingerprint,
            "finding_id": finding.finding_id,
            "rule": finding.rule,
            "finding_type": finding.finding_type,
            "content_identifier": finding.content_identifier,
            "node_id": finding.node_id,
            "reviewer": "Publication Reviewer",
            "reviewed_at": "2026-08-27T21:00:00+00:00",
            "note": "The convergence is intentional and preserves the required evidence.",
        }
        values.update(changes)
        review = WorkflowPublicationReasoningReview.create(**values)
        self.repository.add(review)
        return review

    def test_each_current_finding_requires_exact_acceptance(self):
        initial = self.project()
        self.assertEqual(len(initial.reasoning_reviews), 2)
        self.assertEqual(initial.publication_review_state, NOT_READY)
        self.assertTrue(all(item.review_status == "pending" for item in initial.reasoning_reviews))

        self.accept(initial.reasoning_reviews[0])
        partial = self.project()
        self.assertEqual([item.review_status for item in partial.reasoning_reviews], ["accepted", "pending"])
        self.assertEqual(partial.publication_review_state, NOT_READY)

        self.accept(partial.reasoning_reviews[1])
        complete = self.project()
        self.assertTrue(all(item.review_status == "accepted" for item in complete.reasoning_reviews))
        self.assertEqual(complete.publication_review_state, READY_FOR_PUBLICATION_REVIEW)
        self.assertEqual(len(complete.validation.reasoning_findings), 2)
        html = self.client.get("/workflow-editor/review_demo.json").get_data(as_text=True)
        self.assertIn("2 reasoning findings reviewed", html)
        self.assertIn("Reasoning review complete", html)
        self.assertIn("All 2 deterministic reasoning findings have been explicitly reviewed", html)
        self.assertIn("View publication review", html)

    def test_stale_fingerprint_and_identity_mismatches_fail_closed(self):
        initial = self.project()
        first = initial.reasoning_reviews[0]
        self.accept(first, finding_id="CUR-AAAAAAAAAAAA")
        self.assertEqual(self.project().reasoning_reviews[0].review_status, "pending")

        current = json.loads((self.draft_dir / "review_demo.json").read_text(encoding="utf-8"))
        self.accept(first)
        current["description"] = "A later authored wording change."
        self._write_draft(current)
        stale = self.project()
        self.assertEqual(stale.publication_review_state, NOT_READY)
        self.assertEqual(stale.reasoning_reviews[0].review_status, "stale")
        html = self.client.get(
            "/workflow-editor/review_demo.json"
        ).get_data(as_text=True)
        self.assertIn("workflow-publication-review__status--stale", html)
        self.assertIn("This finding was reviewed against an earlier draft and must be reviewed again.", html)
        self.assertIn("Review again", html)

    def test_wrong_rule_type_and_node_do_not_satisfy_current_finding(self):
        finding = self.project().reasoning_reviews[0]
        cases = (
            {"rule": "CUR-WR-SIGNAL-RETENTION"},
            {"finding_type": "workflow_reasoning_signal_loss"},
            {"node_id": "other_node", "content_identifier": "review_demo:other_node"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.accept(finding, **changes)
        self.assertEqual(self.project().reasoning_reviews[0].review_status, "pending")

    def test_malformed_and_duplicate_exact_records_fail_closed(self):
        finding = self.project().reasoning_reviews[0]
        self.accept(finding)
        self.accept(finding)
        duplicate = self.project()
        self.assertEqual(duplicate.publication_review_state, NOT_READY)
        self.assertIn("Multiple publication-review acceptances", duplicate.reasoning_review_error)

        directory = self.root / "curation_memory" / "workflow_publication_reviews" / "review_demo"
        (directory / "WPR-0000000000000000.json").write_text("{}\n", encoding="utf-8")
        malformed = self.project()
        self.assertEqual(malformed.publication_review_state, NOT_READY)
        self.assertTrue(malformed.reasoning_review_error)

    def test_wrong_workflow_record_fails_closed(self):
        finding = self.project().reasoning_reviews[0]
        review = WorkflowPublicationReasoningReview.create(
            workflow_id="other_workflow",
            draft_semantic_fingerprint=self.project().draft_semantic_fingerprint,
            finding_id=finding.finding_id,
            rule=finding.rule,
            finding_type=finding.finding_type,
            content_identifier=f"other_workflow:{finding.node_id}",
            node_id=finding.node_id,
            reviewer="Publication Reviewer",
            reviewed_at="2026-08-27T21:00:00+00:00",
            note="This deliberately mismatched fixture must fail closed.",
        )
        directory = self.root / "curation_memory" / "workflow_publication_reviews" / "review_demo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{review.review_id}.json").write_text(
            json.dumps(review.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        result = self.project()
        self.assertEqual(result.publication_review_state, NOT_READY)
        self.assertIn("workflow identity is inconsistent", result.reasoning_review_error)

    def test_disappeared_finding_is_not_required_and_new_finding_is_required(self):
        initial = self.project()
        for finding in initial.reasoning_reviews:
            self.accept(finding)
        changed = json.loads((self.draft_dir / "review_demo.json").read_text(encoding="utf-8"))
        changed["nodes"]["shared"]["next"] = "done"
        for node_id in ("second_branch", "inspect_c", "inspect_d"):
            changed["nodes"].pop(node_id)
        self._write_draft(changed)
        result = self.project()
        self.assertEqual(result.publication_review_state, NOT_READY)
        self.assertLess(len(result.reasoning_reviews), len(initial.reasoning_reviews))
        self.assertTrue(all(item.review_status != "accepted" for item in result.reasoning_reviews))

    def test_new_finding_after_review_requires_fresh_acceptance(self):
        initial = self.project()
        for finding in initial.reasoning_reviews:
            self.accept(finding)
        changed = json.loads((self.draft_dir / "review_demo.json").read_text(encoding="utf-8"))
        changed["nodes"]["done"] = {
            "type": "instruction", "title": "Continue review",
            "instruction": "Continue the read-only review.", "next": "third_branch",
        }
        changed["nodes"].update({
            "third_branch": {
                "type": "question", "question": "Which final condition applies?",
                "answers": {
                    "e": {"label": "Condition E", "next": "inspect_e"},
                    "f": {"label": "Condition F", "next": "inspect_f"},
                },
            },
            "inspect_e": {"type": "instruction", "title": "Inspect E", "instruction": "Inspect condition E without changing it.", "next": "complete"},
            "inspect_f": {"type": "instruction", "title": "Inspect F", "instruction": "Inspect condition F without changing it.", "next": "complete"},
            "complete": {"type": "resolution", "title": "Complete", "message": "Review complete."},
        })
        self._write_draft(changed)
        result = self.project()
        self.assertEqual(len(result.reasoning_reviews), 3)
        self.assertEqual(result.publication_review_state, NOT_READY)
        self.assertTrue(any(item.review_status == "pending" for item in result.reasoning_reviews))

    def test_designer_records_only_publication_review_and_renders_status(self):
        before_draft = (self.draft_dir / "review_demo.json").read_bytes()
        before_publication = (self.root / "app" / "workflow_publications" / "review_demo" / "v0001.json").read_bytes()
        memory_path = self.root / "curation_memory" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"marker":"unchanged"}\n', encoding="utf-8")
        before_memory = memory_path.read_bytes()

        page = self.client.get("/workflow-editor/review_demo.json")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(html.count("Accept for this publication"), 2)
        self.assertIn("workflow-publication-review__status--pending", html)
        self.assertIn("Review publication readiness", html)
        self.assertIn("data-close-publication-review", html)
        with self.client.session_transaction() as browser:
            token = browser["workflow_publication_review_csrf"]
        finding = self.project().reasoning_reviews[0]
        response = self.client.post(
            "/workflow-editor/review_demo.json/publication-reasoning-review",
            data={
                "csrf_token": token,
                "finding_id": finding.finding_id,
                "draft_semantic_fingerprint": self.project().draft_semantic_fingerprint,
                "reviewer": "Browser Reviewer",
                "note": "Reviewed and accepted for this exact draft publication.",
            }, follow_redirects=True,
        )
        accepted = response.get_data(as_text=True)
        self.assertIn("Accepted for this publication", accepted)
        self.assertIn("Browser Reviewer", accepted)
        self.assertEqual((self.draft_dir / "review_demo.json").read_bytes(), before_draft)
        self.assertEqual((self.root / "app" / "workflow_publications" / "review_demo" / "v0001.json").read_bytes(), before_publication)
        self.assertEqual(memory_path.read_bytes(), before_memory)

    def test_publication_endpoint_fails_closed_until_reasoning_is_accepted(self):
        publication = self.root / "app" / "workflow_publications" / "review_demo"
        before = {path.name: path.read_bytes() for path in publication.iterdir()}
        response = self.client.post(
            "/api/workflow-drafts/review_demo.json/publication",
            json={"label": "Must remain blocked"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("explicit publication acceptance", response.get_json()["error"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in publication.iterdir()}, before
        )

    def test_missing_csrf_cannot_create_acceptance(self):
        finding = self.project().reasoning_reviews[0]
        response = self.client.post(
            "/workflow-editor/review_demo.json/publication-reasoning-review",
            data={
                "finding_id": finding.finding_id,
                "draft_semantic_fingerprint": self.project().draft_semantic_fingerprint,
                "reviewer": "Browser Reviewer", "note": "No CSRF token is supplied.",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.repository.list_for_workflow("review_demo"), ())

    def test_curator_calibration_task_state_and_verification_do_not_satisfy_gate(self):
        memory = {
            "tasks": {"GKT-ADVISORY": {
                "status": "resolved", "review_disposition": "INTENTIONAL",
                "notes": ["Human note"], "current_verification": {"status": "still_detected"},
            }}
        }
        path = self.root / "curation_memory" / "memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memory), encoding="utf-8")
        before = path.read_bytes()
        result = self.project()
        self.assertEqual(result.publication_review_state, NOT_READY)
        self.assertEqual(path.read_bytes(), before)

    def test_client_marks_rendered_acceptance_details_stale_after_draft_change(self):
        scripts = Path("app/templates/workflow/_workflow_scripts.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("displayedReviewIsStale", scripts)
        self.assertIn("reasoningList.hidden = displayedReviewIsStale", scripts)

    def test_sidebar_summary_and_dedicated_publication_review_workspace(self):
        html = self.client.get(
            "/workflow-editor/review_demo.json"
        ).get_data(as_text=True)
        self.assertIn('id="workflowPublicationReviewSummary"', html)
        self.assertIn("2 reasoning findings require review", html)
        self.assertIn("Validation clean", html)
        self.assertIn('id="reviewPublicationReadinessButton"', html)
        self.assertIn('id="workflowReasoningPublicationReview"', html)
        self.assertIn('class="workflow-publication-workspace"', html)
        self.assertIn("Affected step: Which first condition applies?", html)
        self.assertIn("Why this was flagged", html)
        self.assertIn("What happens here", html)
        self.assertIn("View technical evidence", html)
        self.assertIn("workflow-publication-review__status--pending", html)
        self.assertIn('class="workflow-publication-review__form"', html)
        self.assertIn('name="note" rows="5"', html)
        sidebar = html[html.index('id="workflowPublicationReviewSummary"'):html.index('id="workflowReasoningPublicationReview"')]
        self.assertNotIn("View technical evidence", sidebar)
        self.assertNotIn('name="reviewer"', sidebar)

        styles = Path("app/static/css/workflow_designer.css").read_text(
            encoding="utf-8"
        )
        for rule in (
            '[data-bs-theme="dark"] .workflow-publication-summary',
            '[data-bs-theme="dark"] .workflow-publication-review__form .form-control',
            ".workflow-panel.is-publication-review-hidden { display: none !important; }",
            ".workflow-publication-workspace {",
            ".workflow-publication-finding--accepted",
            ".workflow-publication-finding__technical dl",
            ".workflow-publication-finding details > summary:focus-visible",
            "overflow-wrap: anywhere",
            "min-width: 0",
        ):
            self.assertIn(rule, styles)

        scripts = Path("app/templates/workflow/_workflow_scripts.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("openPublicationReview", scripts)
        self.assertIn("closePublicationReview", scripts)
        self.assertIn('window.location.hash === "#workflowReasoningPublicationReview"', scripts)

    def test_accepted_publication_review_is_compact_and_disclosed(self):
        initial = self.project()
        self.accept(initial.reasoning_reviews[0])
        html = self.client.get(
            "/workflow-editor/review_demo.json"
        ).get_data(as_text=True)
        self.assertIn("workflow-publication-review__status--accepted", html)
        self.assertIn("Accepted for this publication", html)
        self.assertIn("View review", html)
        self.assertIn("View technical evidence", html)
        self.assertIn("The convergence is intentional and preserves the required evidence.", html)


if __name__ == "__main__":
    unittest.main()
