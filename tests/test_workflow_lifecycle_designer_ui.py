import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.workflow_lifecycle_projection_service import (
    AMBIGUOUS_STATE,
    AUTHORED_OR_UNATTRIBUTED_CHANGES,
    GOVERNED_CHANGES,
    MATCHES_PUBLISHED,
    MIXED_CHANGES,
    NO_ACTIVE_PUBLICATION,
    NO_UNPUBLISHED_CHANGES,
    NOT_READY,
    READY_FOR_PUBLICATION_REVIEW,
    SemanticDeltaOperation,
    WorkflowLifecycleProjection,
    WorkflowRuntimeProjection,
    WorkflowValidationProjection,
)


def workflow():
    return {
        "workflow_id": "designer_lifecycle",
        "name": "Designer Lifecycle",
        "description": "Read-only lifecycle UI fixture.",
        "start_node": "inspect",
        "estimated_steps": 2,
        "progress_mode": "branch_aware",
        "nodes": {
            "inspect": {
                "type": "instruction", "title": "Inspect",
                "instruction": "Inspect the current state.", "next": "done",
            },
            "done": {
                "type": "resolution", "title": "Done", "message": "Inspection complete.",
            },
        },
    }


def projection():
    return WorkflowLifecycleProjection(
        lifecycle_state=MATCHES_PUBLISHED,
        publication_review_state=NO_UNPUBLISHED_CHANGES,
        workflow_id="designer_lifecycle",
        draft_filename="designer_lifecycle.json",
        draft_path="app/workflow_drafts/designer_lifecycle.json",
        draft_raw_fingerprint="a" * 64,
        draft_semantic_fingerprint="b" * 64,
        active_published_version=2,
        published_semantic_fingerprint="b" * 64,
        runtime=WorkflowRuntimeProjection(2, True, False),
        semantic_delta=(),
        governed_delta_summary=(),
        authored_or_unattributed_delta_summary=(),
        validation=WorkflowValidationProjection(True, True, "CLEAN", (), (), (), ()),
        readiness_reasons=(),
        ambiguity_reasons=(),
        evaluated_at="2026-08-26T12:00:00+00:00",
    )


def delta(path, provenance, before='"before"', after='"after"'):
    return SemanticDeltaOperation(
        "replace", path, before, after, "c" * 64, "d" * 64,
        provenance, ("SRX-1111111111111111",) if provenance == "governed" else (),
    )


class WorkflowLifecycleDesignerUITests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def render(self, value):
        draft = workflow()
        with patch("app.app.WorkflowDraftService") as draft_type, patch(
            "app.app.WorkflowLifecycleProjectionService"
        ) as projection_type:
            draft_type.return_value.get_draft.return_value = draft
            projection_type.return_value.project.return_value = value
            response = self.client.get("/workflow-editor/designer_lifecycle.json")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_matches_published_is_clear_and_existing_controls_remain(self):
        html = self.render(projection())
        self.assertIn("Matches published · Published v2", html)
        self.assertIn("NO UNPUBLISHED CHANGES", html)
        self.assertIn("No unpublished changes.", html)
        self.assertIn("Aligned", html)
        for control in (
            'id="simulateWorkflowButton"', 'id="validateWorkflowButton"',
            'id="publishWorkflowButton"', 'id="nodeEditorForm"',
        ):
            self.assertIn(control, html)

    def test_governed_authored_and_mixed_changes_are_distinguished(self):
        governed = delta("/progress_mode", "governed", "null", '"branch_aware"')
        authored = delta("/description", "authored_or_unattributed")
        cases = (
            (
                replace(
                    projection(), lifecycle_state=GOVERNED_CHANGES,
                    publication_review_state=READY_FOR_PUBLICATION_REVIEW,
                    semantic_delta=(governed,), draft_semantic_fingerprint="d" * 64,
                ),
                ("Governed unpublished changes · Published v2",
                 'id="workflowLifecycleGovernedCount">1', "Governed"),
            ),
            (
                replace(
                    projection(), lifecycle_state=AUTHORED_OR_UNATTRIBUTED_CHANGES,
                    publication_review_state=READY_FOR_PUBLICATION_REVIEW,
                    semantic_delta=(authored,), draft_semantic_fingerprint="d" * 64,
                ),
                ("Authored/unattributed changes · Published v2",
                 'id="workflowLifecycleAuthoredCount">1',
                 "not eligible for future automated publication"),
            ),
            (
                replace(
                    projection(), lifecycle_state=MIXED_CHANGES,
                    publication_review_state=READY_FOR_PUBLICATION_REVIEW,
                    semantic_delta=(governed, authored), draft_semantic_fingerprint="d" * 64,
                ),
                ("Mixed unpublished changes · Published v2",
                 'id="workflowLifecycleGovernedCount">1',
                 'id="workflowLifecycleAuthoredCount">1'),
            ),
        )
        for value, expected in cases:
            with self.subTest(state=value.lifecycle_state):
                html = self.render(value)
                self.assertIn("READY FOR PUBLICATION REVIEW", html)
                for text in expected:
                    self.assertIn(text, html)

    def test_delta_categories_and_bounded_summaries_render(self):
        changes = (
            delta("/progress_mode", "governed"),
            delta("/nodes/check/title", "authored_or_unattributed"),
            delta("/nodes/check/answers/yes/next", "governed"),
            delta("/nodes/check/knowledge_article", "authored_or_unattributed"),
            delta("/presentation/theme/name", "authored_or_unattributed"),
        )
        value = replace(
            projection(), lifecycle_state=MIXED_CHANGES,
            publication_review_state=READY_FOR_PUBLICATION_REVIEW,
            semantic_delta=changes, draft_semantic_fingerprint="d" * 64,
        )
        html = self.render(value)
        for category in (
            "Workflow metadata", "Node", "Route/transition",
            "Knowledge relationship", "Other workflow field",
        ):
            self.assertIn(category, html)
        self.assertIn("Review <span id=\"workflowLifecycleDetailsCount\">5</span>", html)
        self.assertNotIn("c" * 64, html)

    def test_ambiguous_not_ready_and_no_publication_states(self):
        ambiguous = replace(
            projection(), lifecycle_state=AMBIGUOUS_STATE,
            publication_review_state=NOT_READY,
            readiness_reasons=("Lifecycle or provenance state is ambiguous.",),
            ambiguity_reasons=("Multiple editable drafts claim this workflow identity.",),
        )
        html = self.render(ambiguous)
        self.assertIn("Ambiguous lifecycle state", html)
        self.assertIn("Lifecycle state could not be determined safely.", html)
        self.assertIn("Multiple editable drafts claim this workflow identity.", html)
        self.assertIn("NOT READY FOR PUBLICATION REVIEW", html)

        no_publication = replace(
            projection(), lifecycle_state=NO_ACTIVE_PUBLICATION,
            publication_review_state=NOT_READY, active_published_version=None,
            published_semantic_fingerprint="", runtime=WorkflowRuntimeProjection(None, False, False),
            readiness_reasons=("No active publication exists for comparison.",),
        )
        html = self.render(no_publication)
        self.assertIn("No active publication", html)
        self.assertIn("No active publication exists for comparison.", html)

    def test_runtime_mismatch_and_overlay_are_disclosed(self):
        value = replace(
            projection(), lifecycle_state=AMBIGUOUS_STATE,
            publication_review_state=NOT_READY,
            runtime=WorkflowRuntimeProjection(1, False, True),
            readiness_reasons=("Runtime selection is not coherent with the active publication.",),
            ambiguity_reasons=("Runtime selection does not match the active publication manifest.",),
        )
        html = self.render(value)
        self.assertIn("Not aligned", html)
        self.assertIn("Runtime uses a disclosed compatibility overlay", html)
        self.assertIn("Runtime selection is not coherent", html)

    def test_lifecycle_endpoint_is_read_only_and_returns_bounded_view(self):
        draft = workflow()
        value = replace(
            projection(), lifecycle_state=AUTHORED_OR_UNATTRIBUTED_CHANGES,
            publication_review_state=READY_FOR_PUBLICATION_REVIEW,
            semantic_delta=(delta("/description", "authored_or_unattributed"),),
        )
        with patch("app.app.WorkflowDraftService") as draft_type, patch(
            "app.app.WorkflowLifecycleProjectionService"
        ) as projection_type:
            draft_type.return_value.get_draft.return_value = draft
            projection_type.return_value.project.return_value = value
            response = self.client.get(
                "/api/workflow-drafts/designer_lifecycle.json/lifecycle"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["view"]["change_count"], 1)
        self.assertTrue(payload["view"]["has_unattributed_changes"])
        draft_type.return_value.update_node.assert_not_called()
        draft_type.return_value.update_settings.assert_not_called()
        self.assertNotIn("auto_publish", payload)

    def test_client_refreshes_read_only_projection_after_existing_actions(self):
        scripts = Path("app/templates/workflow/_workflow_scripts.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("const loadLifecycleProjection = async () =>", scripts)
        self.assertGreaterEqual(scripts.count("loadLifecycleProjection();"), 4)
        self.assertNotIn("autoPublish", scripts)
        self.assertNotIn("resolveTask", scripts)


if __name__ == "__main__":
    unittest.main()
