import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app
from app.services.curator_confusing_step_improvement_service import (
    CuratorConfusingStepImprovementError,
    CuratorConfusingStepImprovementService,
)
from app.services.curator_content_quality_bridge_service import CuratorContentQualityBridgeService
from app.services.curator_task_service import CuratorTaskService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import WorkflowPublicationService
from curator.memory import CuratorMemoryStore


class CuratorConfusingStepImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "app").mkdir()
        self.history_path = self.root / "runtime_history"
        self.drafts = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        self.publications = WorkflowPublicationService(self.root / "app" / "workflow_publications")
        self.workflow = {
            "workflow_id": "advanced_network",
            "name": "Advanced Network Diagnostics",
            "description": "Diagnose DNS safely.",
            "category": "Networking",
            "platform": "Windows",
            "estimated_steps": 2,
            "start_node": "test_dns",
            "nodes": {
                "test_dns": {
                    "type": "instruction", "title": "Test DNS",
                    "instruction": "Run the approved DNS check.",
                    "help_text": "Original help.", "next": "done",
                },
                "done": {"type": "resolution", "title": "Done", "message": "Finished."},
            },
        }
        self.filename = self.drafts.save_draft(self.workflow)
        self.publications.publish(self.workflow, self.filename, label="Baseline")
        self.bridge = CuratorContentQualityBridgeService(self.root)
        self.task = self.bridge.send({
            "kind": "confusing_step",
            "quality_rule": "CQ-FREQUENTLY-CONFUSING-STEP",
            "workflow_id": "advanced_network",
            "workflow_version": 1,
            "node_id": "test_dns",
            "priority": "medium",
            "report_count": 2,
            "sample_count": 3,
            "aggregate_clarity": 2.0,
            "measured_at": "2026-08-19T20:00:00+00:00",
            "comment": "private comment",
        })
        self.service = CuratorConfusingStepImprovementService(
            self.root, history_path=self.history_path
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_confusing_step_is_eligible(self):
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"]["GKT-OTHER"] = {
            **self.task, "task_id": "GKT-OTHER", "finding_type": "clarity",
        }
        store.save(state)
        with self.assertRaises(CuratorConfusingStepImprovementError):
            self.service.prepare("GKT-OTHER")

    def test_prepare_is_stable_private_and_changes_no_workflow_or_publication(self):
        draft_path = self.root / "app" / "workflow_drafts" / self.filename
        published_path = self.root / "app" / "workflow_publications" / "advanced_network" / "v0001.json"
        before = (draft_path.read_bytes(), published_path.read_bytes())
        first = self.service.prepare(self.task["task_id"])
        second = self.service.prepare(self.task["task_id"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(first["proposal_type"], "confusing_step_help_text")
        self.assertEqual(first["before_workflow_version"], 1)
        self.assertEqual(first["current_help_text"], "Original help.")
        self.assertEqual(first["proposed_help_text"], "Original help.")
        self.assertNotIn("private comment", json.dumps(first))
        self.assertEqual((draft_path.read_bytes(), published_path.read_bytes()), before)

    def test_prepare_backfills_authoritative_version_for_an_existing_task(self):
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"][self.task["task_id"]]["quality_baseline"]["workflow_version"] = None
        store.save(state)
        history = TroubleshootingHistoryService(self.history_path)
        self._feedback(history, "advanced_network", 1, "test_dns", 2, "test_dns")
        package = self.service.prepare(self.task["task_id"])
        self.assertEqual(package["before_workflow_version"], 1)
        self.assertEqual(package["quality_baseline"]["workflow_version"], 1)

    def test_human_approval_is_required_and_does_not_edit_or_publish(self):
        self.service.prepare(self.task["task_id"])
        with self.assertRaises(CuratorConfusingStepImprovementError):
            self.service.handoff(self.task["task_id"])
        with self.assertRaises(CuratorConfusingStepImprovementError):
            self.service.approve(self.task["task_id"], reviewer="", note="")
        package = self.service.edit(self.task["task_id"], "Use the DNS server name shown above.")
        draft_before = self.drafts.get_draft(self.filename)
        versions_before = self.publications.status("advanced_network")["current_version"]
        approved = self.service.approve(
            self.task["task_id"], reviewer="Alex Reviewer",
            note="Clearer context for the command."
        )
        self.assertEqual(approved["status"], "human_approved")
        self.assertEqual(approved["approved_by"], "Alex Reviewer")
        self.assertTrue(approved["approved_at"])
        self.assertEqual(self.service.handoff(self.task["task_id"])["node_id"], "test_dns")
        self.assertEqual(self.drafts.get_draft(self.filename), draft_before)
        self.assertEqual(self.publications.status("advanced_network")["current_version"], versions_before)
        self.assertEqual(package["proposed_help_text"], "Use the DNS server name shown above.")

    def test_task_ui_hides_handoff_until_human_approval(self):
        task_service = CuratorTaskService(self.root)
        resolution_service = Mock()
        resolution_service.get.return_value = None
        app.config.update(TESTING=True)
        with patch("app.app.CuratorTaskService", return_value=task_service), \
                patch("app.app.CuratorResolutionService", return_value=resolution_service), \
                patch("app.app.CuratorConfusingStepImprovementService", return_value=self.service):
            client = app.test_client()
            before = client.get(f"/curator/tasks/{self.task['task_id']}").get_data(as_text=True)
            self.assertIn("Prepare help-text proposal", before)
            self.assertNotIn("Open affected step in Workflow Designer", before)
            self.service.prepare(self.task["task_id"])
            proposed = client.get(f"/curator/tasks/{self.task['task_id']}").get_data(as_text=True)
            self.assertIn("Proposed help text", proposed)
            self.assertNotIn("Open affected step in Workflow Designer", proposed)
            self.service.edit(self.task["task_id"], "Clearer DNS context.")
            self.service.approve(
                self.task["task_id"], reviewer="Alex Reviewer", note="Reviewed for clarity."
            )
            approved = client.get(f"/curator/tasks/{self.task['task_id']}").get_data(as_text=True)
            self.assertIn("Open affected step in Workflow Designer", approved)
            self.assertIn("Alex Reviewer", approved)

    def test_publication_association_validates_version_node_and_approved_wording(self):
        self._approve("Clearer DNS context.")
        with self.assertRaises(CuratorConfusingStepImprovementError):
            self.service.record_published_version(self.task["task_id"])
        self.drafts.update_node(self.filename, "test_dns", {"help_text": "Different wording."})
        self.publications.publish(self.drafts.get_draft(self.filename), self.filename, label="Wrong wording")
        with self.assertRaises(CuratorConfusingStepImprovementError):
            self.service.record_published_version(self.task["task_id"])
        self.drafts.update_node(self.filename, "test_dns", {"help_text": "Clearer DNS context."})
        self.publications.publish(self.drafts.get_draft(self.filename), self.filename, label="Approved wording")
        package = self.service.record_published_version(self.task["task_id"])
        self.assertEqual(package["published_version"], 3)
        self.assertEqual(package["status"], "published")
        self.assertTrue(package["workflow_changed_event_id"])
        events = CuratorMemoryStore(self.root / "curation_memory").load()["growth"]["event_queue"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "workflow_changed")
        self.assertEqual(events[0]["metadata"]["task_id"], self.task["task_id"])
        self.assertEqual(events[0]["metadata"]["before_version"], 1)
        self.assertEqual(events[0]["metadata"]["after_version"], 3)
        self.assertEqual(self.service.record_published_version(self.task["task_id"])["version"], package["version"])

    def test_measurement_is_version_node_isolated_correct_and_idempotent(self):
        self._approve_and_publish("Clearer DNS context.")
        history = TroubleshootingHistoryService(self.history_path)
        self._feedback(history, "advanced_network", 1, "test_dns", 2, "test_dns")
        self._feedback(history, "advanced_network", 1, "test_dns", 4, None)
        self._feedback(history, "advanced_network", 2, "test_dns", 5, None)
        self._feedback(history, "advanced_network", 2, "test_dns", 4, None)
        self._feedback(history, "advanced_network", 2, "other_node", 1, "other_node")
        self._feedback(history, "other_workflow", 2, "test_dns", 1, "test_dns")
        self._feedback(
            history, "advanced_network", 2, "test_dns", 1, "test_dns",
            environment="test",
        )
        measured = self.service.measure(self.task["task_id"])
        measurement = measured["measurement"]
        self.assertEqual(measurement["state"], "observational_evidence_available")
        self.assertEqual(measurement["before"]["sample_count"], 2)
        self.assertEqual(measurement["before"]["confusing_step_count"], 1)
        self.assertEqual(measurement["before"]["confusing_rate"], 50.0)
        self.assertEqual(measurement["before"]["aggregate_clarity"], 3.0)
        self.assertEqual(measurement["after"]["sample_count"], 2)
        self.assertEqual(measurement["after"]["confusing_step_count"], 0)
        self.assertEqual(measurement["after"]["confusing_rate"], 0.0)
        self.assertEqual(measurement["after"]["aggregate_clarity"], 4.5)
        self.assertEqual(measurement["confusing_rate_change_points"], -50.0)
        self.assertEqual(measurement["aggregate_clarity_change"], 1.5)
        self.assertEqual(self.service.measure(self.task["task_id"])["version"], measured["version"])

    def test_insufficient_post_change_evidence_is_explicit(self):
        self._approve_and_publish("Clearer DNS context.")
        history = TroubleshootingHistoryService(self.history_path)
        self._feedback(history, "advanced_network", 1, "test_dns", 2, "test_dns")
        self._feedback(history, "advanced_network", 2, "test_dns", 5, None)
        measurement = self.service.measure(self.task["task_id"])["measurement"]
        self.assertEqual(measurement["state"], "insufficient_post_change_evidence")
        self.assertEqual(measurement["label"], "Insufficient post-change evidence")

    def _approve(self, text):
        self.service.prepare(self.task["task_id"])
        self.service.edit(self.task["task_id"], text)
        return self.service.approve(
            self.task["task_id"], reviewer="Alex Reviewer", note="Reviewed for clarity."
        )

    def _approve_and_publish(self, text):
        self._approve(text)
        self.drafts.update_node(self.filename, "test_dns", {"help_text": text})
        self.publications.publish(self.drafts.get_draft(self.filename), self.filename, label="Approved help text")
        return self.service.record_published_version(self.task["task_id"])

    @staticmethod
    def _feedback(history, workflow_id, version, node_id, clarity, confusing_step,
                  environment="production"):
        record = history.start(
            workflow_id, workflow_id, node_id, version=version,
            session_environment=environment,
        )
        history.complete(record["id"], node_id)
        history.add_feedback(record["id"], {
            "solved": "yes", "clarity": clarity,
            "confusing_step": confusing_step, "comment": "private runtime comment",
        })


if __name__ == "__main__":
    unittest.main()
