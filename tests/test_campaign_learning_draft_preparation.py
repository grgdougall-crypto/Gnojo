import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.services.campaign_learning_draft_preparation_service import (
    CampaignLearningDraftPreparationError,
    CampaignLearningDraftPreparationService,
)
from app.services.workflow_help_text_service import WorkflowHelpTextService


class Planner:
    def __init__(self, campaign):
        self.campaign = campaign

    def get(self, campaign_id):
        if campaign_id != self.campaign["campaign_id"]:
            raise AssertionError(campaign_id)
        return deepcopy(self.campaign)


class Provider:
    model = "test-model-v1"

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = 0

    def generate_workflow_node_suggestion(self, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return {"help_text": self.responses.pop(0)}


def valid_help(subject):
    return (
        f"Compare the displayed {subject} with the value observed before this step. "
        "Record the exact result so the reviewer can distinguish the reported condition without assuming a permanent resolution."
    )


class CampaignLearningDraftPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign_root = self.root / "knowledge_campaigns"
        self.orchestration_root = self.campaign_root / "orchestration"
        self.draft_root = self.root / "app" / "workflow_drafts"
        self.orchestration_root.mkdir(parents=True)
        self.draft_root.mkdir(parents=True)
        self.campaign_id = "KCP-LEARNING1"
        self.orchestration_id = "KORCH-LEARNING1"
        self.work_item_id = "KCW-LEARNING1"
        self.workflow_id = "low_storage"
        self.workflow = {
            "workflow_id": self.workflow_id,
            "name": "Low Disk Space",
            "start_node": "inspect_space",
            "nodes": {
                "inspect_space": {
                    "type": "question",
                    "question": "How much free space is displayed?",
                    "answers": {"low": {"label": "Very little", "next": "review_files"}},
                },
                "review_files": {
                    "type": "instruction",
                    "title": "Review storage categories",
                    "instruction": "Observe which storage category uses the most space.",
                    "help_text": "Existing reviewed guidance remains exactly as written.",
                    "next": "done",
                },
                "new_current_node": {
                    "type": "instruction",
                    "title": "Observe file sizes",
                    "instruction": "Observe the sizes of the largest personal files.",
                    "next": "done",
                },
                "done": {"type": "resolution", "title": "Review complete", "message": "Review the evidence."},
            },
        }
        (self.draft_root / "low_storage.json").write_text(
            json.dumps(self.workflow, indent=2) + "\n", encoding="utf-8"
        )
        self.campaign = {
            "campaign_id": self.campaign_id,
            "gaps": [{
                "gap_id": "KCG-LEARNING1",
                "gap_type": "weak_learning_coverage",
            }],
            "work_items": [{
                "work_item_id": self.work_item_id,
                "gap_id": "KCG-LEARNING1",
                "gap_type": "weak_learning_coverage",
                "work_type": "learning_content",
                "workflow_id": self.workflow_id,
                "node_ids": ["stale_campaign_node"],
            }],
        }
        (self.campaign_root / f"{self.campaign_id}.json").write_text(
            json.dumps(self.campaign, indent=2) + "\n", encoding="utf-8"
        )
        self.orchestration = {
            "schema_version": "1.0",
            "orchestration_id": self.orchestration_id,
            "campaign_id": self.campaign_id,
            "status": "awaiting_human_review",
            "mode": "supervised",
            "work_item_states": [{
                "work_item_id": self.work_item_id,
                "state": "awaiting_human_review",
                "action_authority": "human_gate",
                "next_action": "author_learning_content",
            }],
            "history": [],
        }
        self.orchestration_path = self.orchestration_root / f"{self.orchestration_id}.json"
        self.orchestration_path.write_text(
            json.dumps(self.orchestration, indent=2) + "\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def service(self, provider):
        return CampaignLearningDraftPreparationService(
            self.root,
            self.campaign_root,
            planner=Planner(self.campaign),
            help_text=WorkflowHelpTextService(providers=[("OpenAI", provider)]),
            now=lambda: "2026-09-08T12:00:00+00:00",
        )

    def test_current_draft_blanks_are_generated_and_existing_help_is_preserved(self):
        campaign_before = (
            self.campaign_root / f"{self.campaign_id}.json"
        ).read_bytes()
        structural_before = deepcopy(self.workflow)
        for node in structural_before["nodes"].values():
            node.pop("help_text", None)
        provider = Provider([valid_help("free-space amount"), valid_help("file-size evidence")])
        result = self.service(provider).prepare(
            self.campaign_id, self.orchestration_id, self.work_item_id,
            actor="Reviewer",
        )
        saved = json.loads((self.draft_root / "low_storage.json").read_text(encoding="utf-8"))
        self.assertEqual(result["generated_saved"], 2)
        self.assertEqual(provider.calls, 2)
        self.assertNotIn("stale_campaign_node", [item["node_id"] for item in result["generated_nodes"]])
        self.assertIn("new_current_node", [item["node_id"] for item in result["generated_nodes"]])
        self.assertEqual(
            saved["nodes"]["review_files"]["help_text"],
            "Existing reviewed guidance remains exactly as written.",
        )
        provenance = saved["nodes"]["inspect_space"]["help_text_generation"]
        self.assertEqual(provenance["provider"], "OpenAI")
        self.assertEqual(provenance["model"], "test-model-v1")
        self.assertEqual(provenance["campaign_id"], self.campaign_id)
        structural_after = deepcopy(saved)
        for node in structural_after["nodes"].values():
            node.pop("help_text", None)
            node.pop("help_text_generation", None)
        self.assertEqual(structural_after, structural_before)
        persisted = json.loads(self.orchestration_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["work_item_states"], self.orchestration["work_item_states"])
        self.assertEqual(persisted["history"][-1]["event"], "learning_draft_prepared")
        self.assertEqual(persisted["history"][-1]["summary"]["generated_saved"], 2)
        self.assertEqual(
            (self.campaign_root / f"{self.campaign_id}.json").read_bytes(),
            campaign_before,
        )
        self.assertFalse((self.root / "app" / "workflow_publications").exists())

    def test_invalid_or_unavailable_ai_fails_closed_without_local_fallback(self):
        invalid = Provider(["Too short", valid_help("file-size evidence")])
        result = self.service(invalid).prepare(
            self.campaign_id, self.orchestration_id, self.work_item_id,
            actor="Reviewer",
        )
        saved = json.loads((self.draft_root / "low_storage.json").read_text(encoding="utf-8"))
        self.assertNotIn("help_text", saved["nodes"]["inspect_space"])
        self.assertIn("help_text", saved["nodes"]["new_current_node"])
        self.assertEqual(result["generated_saved"], 1)
        self.assertEqual(len(result["skipped_failed_nodes"]), 1)

        other = self.workflow_id = "other_workflow"
        self.campaign["work_items"][0]["workflow_id"] = other
        (self.draft_root / f"{other}.json").write_text(
            json.dumps({**self.workflow, "workflow_id": other}, indent=2) + "\n",
            encoding="utf-8",
        )
        unavailable = Provider(error=RuntimeError("offline"))
        result = self.service(unavailable).prepare(
            self.campaign_id, self.orchestration_id, self.work_item_id,
            actor="Reviewer",
        )
        unchanged = json.loads((self.draft_root / f"{other}.json").read_text(encoding="utf-8"))
        self.assertNotIn("help_text", unchanged["nodes"]["inspect_space"])
        self.assertEqual(result["generated_saved"], 0)
        self.assertTrue(result["skipped_failed_nodes"])

    def test_rerun_does_not_regenerate_or_overwrite_saved_help(self):
        provider = Provider([valid_help("free-space amount"), valid_help("file-size evidence")])
        service = self.service(provider)
        service.prepare(self.campaign_id, self.orchestration_id, self.work_item_id, actor="Reviewer")
        first = (self.draft_root / "low_storage.json").read_bytes()
        history_count = len(json.loads(self.orchestration_path.read_text())["history"])
        second = service.prepare(self.campaign_id, self.orchestration_id, self.work_item_id, actor="Reviewer")
        self.assertEqual(second["status"], "no_changes")
        self.assertEqual(provider.calls, 2)
        self.assertEqual((self.draft_root / "low_storage.json").read_bytes(), first)
        self.assertEqual(len(json.loads(self.orchestration_path.read_text())["history"]), history_count)

    def test_missing_editable_draft_fails_closed(self):
        (self.draft_root / "low_storage.json").unlink()
        with self.assertRaisesRegex(
            CampaignLearningDraftPreparationError, "exactly one editable workflow draft"
        ):
            self.service(Provider([])).preview(
                self.campaign_id, self.orchestration_id, self.work_item_id
            )

    def test_preview_and_get_are_read_only_and_preserve_exact_context(self):
        provider = Provider([])
        service = self.service(provider)
        before = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        preview = service.preview(self.campaign_id, self.orchestration_id, self.work_item_id)
        self.assertEqual(preview["eligible_count"], 2)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(before, sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        ))
        flask_app.config.update(TESTING=True)
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch(
                "app.app.CampaignLearningDraftPreparationService",
                return_value=service,
            ),
        ):
            response = flask_app.test_client().get(
                f"/curator/growth/orchestration/{self.orchestration_id}/items/"
                f"{self.work_item_id}/learning-draft?campaign_id={self.campaign_id}"
            )
        self.assertEqual(response.status_code, 200)
        rendered = response.get_data(as_text=True)
        self.assertIn("Prepare Learning Draft", rendered)
        self.assertIn("Low Disk Space", rendered)
        self.assertIn(self.campaign_id, rendered)
        self.assertEqual(before, sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        ))

    def test_supervised_post_prepares_draft_and_keeps_campaign_return_context(self):
        provider = Provider([
            valid_help("free-space amount"),
            valid_help("sizes of the largest personal files"),
        ])
        service = self.service(provider)
        flask_app.config.update(TESTING=True)
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch(
                "app.app.CampaignLearningDraftPreparationService",
                return_value=service,
            ),
        ):
            response = flask_app.test_client().post(
                f"/curator/growth/orchestration/{self.orchestration_id}/items/"
                f"{self.work_item_id}/learning-draft",
                data={"campaign_id": self.campaign_id},
            )
        rendered = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Generated and saved", rendered)
        self.assertIn("Inspect Workflow Draft", rendered)
        self.assertIn(
            f"/workflow-editor/low_storage.json?return_to="
            f"/curator/growth/coverage-campaigns/{self.campaign_id}/orchestration",
            rendered.replace("&amp;", "&"),
        )
        persisted = json.loads(self.orchestration_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["work_item_states"][0]["next_action"],
            "author_learning_content",
        )

if __name__ == "__main__":
    unittest.main()
