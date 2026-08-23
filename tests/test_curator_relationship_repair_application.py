import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.curator_relationship_repair_application_service import (
    CuratorRelationshipRepairApplicationError,
    CuratorRelationshipRepairApplicationService,
)
from curator.memory import CuratorMemoryStore


class CuratorRelationshipRepairApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CuratorRelationshipRepairApplicationService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def fixture(self, *, outcome="add", status="open"):
        command_id, article_id = "sample-command", "sample-article"
        if outcome == "remove":
            command_summary = "Tests a remote host using ICMP echo requests and response time."
            article_overview = "Verify a physical Ethernet connection using cable and link lights."
        elif outcome == "human":
            command_summary, article_overview = "Shows diagnostic details.", "General troubleshooting guidance."
        else:
            command_summary = "Shows adapter link status and link speed."
            article_overview = "Check a wired Ethernet physical connection and link lights."
        command = {
            "id": command_id, "title": "Sample command", "name": command_id,
            "summary": command_summary, "category": "Diagnostics", "tags": [],
            "related_articles": [article_id], "related_commands": [], "keep": {"command": True},
        }
        article = {
            "id": article_id, "canonical_id": article_id, "title": "Sample article",
            "overview": article_overview, "category": "Diagnostics", "tags": [],
            "related_commands": [], "commands": [], "keep": {"article": True},
        }
        command_path = self.write(f"knowledge_base/commands/{command_id}.json", command)
        article_path = self.write(f"knowledge_base/published/{article_id}.json", article)
        task = {
            "task_id": "GKT-REL", "title": "Relationship task", "status": status,
            "owner": "Curator", "priority": "Medium", "classification": "Defect",
            "curator_rule": "CUR-REL-ARTICLE-COMMAND-RECIPROCITY-001",
            "finding_type": "article_command_reciprocity_conflict", "content_type": "command",
            "content_identifier": command_id,
            "evidence": [f"Article: {article_id}", f"Command: {command_id}"], "history": [],
        }
        CuratorMemoryStore(self.root / "curation_memory").save({"tasks": {"GKT-REL": task}})
        current_task, proposal = self.service._current_proposal("GKT-REL")
        token = self.service.approval_token(current_task, proposal) if proposal["outcome"] != "human_review_required" else ""
        return command_path, article_path, proposal, token

    def test_add_reciprocal_applies_exact_field_verifies_and_keeps_task_open(self):
        command_path, article_path, proposal, token = self.fixture()
        command_before = json.loads(command_path.read_text(encoding="utf-8"))
        result = self.service.apply("GKT-REL", approval_token=token, approved=True)
        article = json.loads(article_path.read_text(encoding="utf-8"))
        self.assertEqual(article["related_commands"], ["sample-command"])
        self.assertEqual(article["keep"], {"article": True})
        self.assertEqual(json.loads(command_path.read_text(encoding="utf-8")), command_before)
        self.assertEqual(result["verification"]["status"], "appears_corrected")
        task = CuratorMemoryStore(self.root / "curation_memory").load()["tasks"]["GKT-REL"]
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["current_verification"]["status"], "appears_corrected")
        applied = [event for event in task["history"] if event.get("event") == "relationship_repair_proposal_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["metadata"]["before_declarations"], [])
        self.assertEqual(applied[0]["metadata"]["after_declarations"], ["sample-command"])

    def test_remove_unsupported_changes_only_declaring_field(self):
        command_path, article_path, proposal, token = self.fixture(outcome="remove")
        article_before = article_path.read_bytes()
        result = self.service.apply("GKT-REL", approval_token=token, approved=True)
        command = json.loads(command_path.read_text(encoding="utf-8"))
        self.assertEqual(command["related_articles"], [])
        self.assertEqual(command["keep"], {"command": True})
        self.assertEqual(article_path.read_bytes(), article_before)
        self.assertEqual(result["verification"]["status"], "appears_corrected")

    def test_human_review_required_has_no_apply_path(self):
        command_path, article_path, proposal, _ = self.fixture(outcome="human")
        before = (command_path.read_bytes(), article_path.read_bytes())
        self.assertEqual(proposal["outcome"], "human_review_required")
        with self.assertRaises(CuratorRelationshipRepairApplicationError):
            self.service.apply("GKT-REL", approval_token="anything", approved=True)
        self.assertEqual((command_path.read_bytes(), article_path.read_bytes()), before)

    def test_stale_closed_unapproved_and_duplicate_submissions_fail_closed(self):
        _, article_path, _, token = self.fixture()
        with self.assertRaises(CuratorRelationshipRepairApplicationError):
            self.service.apply("GKT-REL", approval_token=token, approved=False)
        article = json.loads(article_path.read_text(encoding="utf-8"))
        article["unrelated_change"] = True
        article_path.write_text(json.dumps(article), encoding="utf-8")
        with self.assertRaises(CuratorRelationshipRepairApplicationError):
            self.service.apply("GKT-REL", approval_token=token, approved=True)

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CuratorRelationshipRepairApplicationService(self.root)
        _, _, _, token = self.fixture(status="resolved")
        with self.assertRaises(CuratorRelationshipRepairApplicationError):
            self.service.apply("GKT-REL", approval_token=token, approved=True)

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CuratorRelationshipRepairApplicationService(self.root)
        _, _, _, token = self.fixture()
        self.service.apply("GKT-REL", approval_token=token, approved=True)
        with self.assertRaises(CuratorRelationshipRepairApplicationError):
            self.service.apply("GKT-REL", approval_token=token, approved=True)

    def test_verification_runs_only_after_write_and_failure_rolls_back_content_and_memory(self):
        _, article_path, _, token = self.fixture()
        memory_path = self.root / "curation_memory" / "memory.json"
        content_before, memory_before = article_path.read_bytes(), memory_path.read_bytes()
        with patch.object(self.service.verifier, "verify", side_effect=RuntimeError("verification failed")) as verify:
            with self.assertRaises(RuntimeError):
                self.service.apply("GKT-REL", approval_token=token, approved=True)
        verify.assert_called_once_with("GKT-REL")
        self.assertEqual(article_path.read_bytes(), content_before)
        self.assertEqual(memory_path.read_bytes(), memory_before)

    def test_ui_exposes_apply_only_for_eligible_actionable_proposal(self):
        _, _, _, token = self.fixture()
        from app.services.curator_task_service import CuratorTaskService
        task_service = CuratorTaskService(self.root)
        client = app.test_client()
        with patch("app.app.CuratorTaskService", return_value=task_service), patch(
                "app.app.CuratorResolutionService.get", return_value=None), patch(
                "app.app.CuratorConfusingStepImprovementService.get", return_value=None):
            html = client.get("/curator/tasks/GKT-REL").get_data(as_text=True)
        self.assertIn("Apply proposed relationship repair", html)
        self.assertIn("I reviewed this exact metadata change", html)
        self.assertIn(token, html)

    def test_ui_withholds_apply_for_human_review_and_closed_tasks(self):
        from app.services.curator_task_service import CuratorTaskService
        client = app.test_client()
        for outcome, status, expected in (
            ("human", "open", "No apply action is available"),
            ("add", "resolved", "cannot be applied in the task's current state"),
        ):
            with self.subTest(outcome=outcome, status=status):
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory()
                self.root = Path(self.temporary.name)
                self.service = CuratorRelationshipRepairApplicationService(self.root)
                self.fixture(outcome=outcome, status=status)
                task_service = CuratorTaskService(self.root)
                with patch("app.app.CuratorTaskService", return_value=task_service), patch(
                        "app.app.CuratorResolutionService.get", return_value=None), patch(
                        "app.app.CuratorConfusingStepImprovementService.get", return_value=None):
                    html = client.get("/curator/tasks/GKT-REL").get_data(as_text=True)
                self.assertNotIn("Apply proposed relationship repair", html)
                self.assertIn(expected, html)

    def test_post_uses_explicit_approval_and_does_not_resolve_task(self):
        _, article_path, _, token = self.fixture()
        client = app.test_client()
        with patch("app.app.CuratorRelationshipRepairApplicationService", return_value=self.service):
            response = client.post("/curator/tasks/GKT-REL/relationship-proposal/apply", data={
                "approval_token": token, "approved": "yes",
                "return_to": "/curator/relationship-proposals?outcome=add_reciprocal",
            })
        self.assertEqual(response.status_code, 302)
        self.assertIn("status=relationship_applied", response.location)
        self.assertEqual(json.loads(article_path.read_text(encoding="utf-8"))["related_commands"],
                         ["sample-command"])
        self.assertEqual(CuratorMemoryStore(self.root / "curation_memory").load()["tasks"]["GKT-REL"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
