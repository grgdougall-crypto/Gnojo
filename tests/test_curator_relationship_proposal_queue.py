import json
import html as html_module
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.curator_relationship_proposal_queue_service import (
    CuratorRelationshipProposalQueueService,
)
from curator.memory import CuratorMemoryStore


class CuratorRelationshipProposalQueueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def command(self, identifier, article, summary):
        return {
            "id": identifier, "title": identifier.replace("-", " ").title(), "name": identifier,
            "summary": summary, "category": "Diagnostics", "platforms": ["Windows 11"],
            "tags": [], "related_articles": [article], "related_commands": [],
        }

    def article(self, identifier, overview):
        return {
            "id": identifier, "canonical_id": identifier, "title": identifier.replace("-", " ").title(),
            "overview": overview, "category": "Diagnostics", "tags": [],
            "related_commands": [], "commands": [],
        }

    @staticmethod
    def task(task_id, command_id, article_id, *, status="open",
             finding_type="article_command_reciprocity_conflict"):
        return {
            "task_id": task_id, "title": "Relationship task", "status": status,
            "priority": "Medium", "owner": "Curator", "classification": "Defect",
            "confidence": "high", "knowledge_debt_score": 1, "finding_type": finding_type,
            "content_type": "command", "content_identifier": command_id,
            "evidence": [f"Article: {article_id}", f"Command: {command_id}"],
            "history": [{"event": "observed", "evidence": [f"Article: {article_id}"]}],
        }

    def build_fixture(self):
        cases = (
            ("GKT-ADD", "adapter-tool", "ethernet-check",
             "Shows adapter link status and link speed.",
             "Check whether an Ethernet cable has a physical connection and link lights.", "open"),
            ("GKT-REMOVE", "reachability-tool", "physical-link",
             "Tests a remote host using ICMP echo requests and response time.",
             "Verify a physical Ethernet connection using cable and link lights.", "in_progress"),
            ("GKT-HUMAN", "generic-tool", "generic-guide",
             "Shows diagnostic details.", "General troubleshooting guidance.", "deferred"),
            ("GKT-RESOLVED", "closed-tool", "closed-guide",
             "Shows adapter link status.", "Check Ethernet link status.", "resolved"),
        )
        tasks = {}
        paths = []
        for task_id, command_id, article_id, summary, overview, status in cases:
            paths.append(self.write(f"knowledge_base/commands/{command_id}.json",
                                    self.command(command_id, article_id, summary)))
            paths.append(self.write(f"knowledge_base/published/{article_id}.json",
                                    self.article(article_id, overview)))
            tasks[task_id] = self.task(task_id, command_id, article_id, status=status)
        tasks["GKT-OTHER"] = self.task(
            "GKT-OTHER", "generic-tool", "generic-guide", finding_type="missing_safety_guidance"
        )
        store = CuratorMemoryStore(self.root / "curation_memory")
        store.save({"tasks": tasks})
        return store, paths

    def test_actionable_supported_tasks_and_outcomes_are_projected(self):
        self.build_fixture()
        queue = CuratorRelationshipProposalQueueService(self.root).queue()
        self.assertEqual(queue["actionable_count"], 3)
        self.assertEqual(queue["closed_count"], 1)
        self.assertEqual({item["task_id"] for item in queue["items"]},
                         {"GKT-ADD", "GKT-REMOVE", "GKT-HUMAN"})
        outcomes = {item["task_id"]: item["outcome"] for item in queue["items"]}
        self.assertEqual(outcomes, {"GKT-ADD": "add_reciprocal",
                                    "GKT-REMOVE": "remove_unsupported",
                                    "GKT-HUMAN": "human_review_required"})
        self.assertTrue(all(item["metadata_change"] for item in queue["items"]))
        self.assertTrue(all(item["change_applied"] is False for item in queue["items"]))

    def test_outcome_and_status_filters_compose(self):
        self.build_fixture()
        service = CuratorRelationshipProposalQueueService(self.root)
        self.assertEqual([item["task_id"] for item in service.queue(outcome="remove_unsupported")["items"]],
                         ["GKT-REMOVE"])
        self.assertEqual([item["task_id"] for item in service.queue(status="deferred")["items"]],
                         ["GKT-HUMAN"])
        self.assertEqual(service.queue(outcome="add_reciprocal", status="deferred")["items"], [])

    def test_queue_projection_does_not_mutate_content_or_task_state(self):
        store, paths = self.build_fixture()
        before_content = {path: path.read_bytes() for path in paths}
        memory_path = self.root / "curation_memory" / "memory.json"
        before_memory = memory_path.read_bytes()
        CuratorRelationshipProposalQueueService(self.root).queue(outcome="add_reciprocal")
        self.assertEqual({path: path.read_bytes() for path in paths}, before_content)
        self.assertEqual(memory_path.read_bytes(), before_memory)
        self.assertEqual(store.load()["tasks"]["GKT-ADD"]["status"], "open")

    def test_no_relationship_tasks_produces_clean_zero_projection(self):
        CuratorMemoryStore(self.root / "curation_memory").save({"tasks": {}})
        queue = CuratorRelationshipProposalQueueService(self.root).queue()
        self.assertEqual(queue["items"], [])
        self.assertEqual(queue["actionable_count"], 0)

    def test_actual_queue_route_detail_and_filtered_return_are_end_to_end_and_read_only(self):
        store, paths = self.build_fixture()
        service = CuratorRelationshipProposalQueueService(self.root)
        client = app.test_client()
        memory_path = self.root / "curation_memory" / "memory.json"
        before_content = {path: path.read_bytes() for path in paths}
        before_memory = memory_path.read_bytes()

        with patch("app.app.CuratorRelationshipProposalQueueService", return_value=service), patch(
                "app.services.curator_targeted_verification_service.CuratorTargetedVerificationService.verify"
        ) as verify:
            all_html = client.get("/curator/relationship-proposals").get_data(as_text=True)
            for task_id, outcome in (("GKT-ADD", "Add Reciprocal"),
                                     ("GKT-REMOVE", "Remove Unsupported"),
                                     ("GKT-HUMAN", "Human Review Required")):
                self.assertIn(task_id, all_html)
                self.assertIn(outcome, all_html)
            self.assertNotIn("GKT-RESOLVED", all_html)
            for expected in ("Command declares article", "Article declares command", "Rationale",
                             "Exact proposed metadata change", "Affected authoritative record",
                             "Proposal only", "Review task"):
                self.assertIn(expected, all_html)

            filtered_path = "/curator/relationship-proposals?outcome=add_reciprocal&status=open"
            filtered_html = client.get(filtered_path).get_data(as_text=True)
            self.assertIn("GKT-ADD", filtered_html)
            self.assertNotIn("GKT-REMOVE", filtered_html)
            self.assertIn('value="add_reciprocal" selected', filtered_html)
            self.assertIn('value="open" selected', filtered_html)
            task_match = re.search(r'href="([^"]*?/curator/tasks/GKT-ADD[^"]*)"', filtered_html)
            self.assertIsNotNone(task_match)
            task_path = html_module.unescape(task_match.group(1))

            with patch("app.app.CuratorTaskService", return_value=service.tasks), patch(
                    "app.app.CuratorResolutionService.get", return_value=None), patch(
                    "app.app.CuratorConfusingStepImprovementService.get", return_value=None):
                detail_html = client.get(task_path).get_data(as_text=True)
            self.assertIn("GKT-ADD", detail_html)
            self.assertIn("Relationship Repair Proposal", detail_html)
            self.assertIn("Return to Relationship Proposals", detail_html)
            return_match = re.search(
                r'href="([^"]+)"><i[^>]*></i>\s*Return to Relationship Proposals', detail_html
            )
            self.assertIsNotNone(return_match)
            return_path = html_module.unescape(return_match.group(1))
            self.assertEqual(return_path, filtered_path)
            returned_html = client.get(return_path).get_data(as_text=True)
            self.assertIn("GKT-ADD", returned_html)
            self.assertIn('value="add_reciprocal" selected', returned_html)
            self.assertIn('value="open" selected', returned_html)
            verify.assert_not_called()

        self.assertEqual({path: path.read_bytes() for path in paths}, before_content)
        self.assertEqual(memory_path.read_bytes(), before_memory)
        after_tasks = store.load()["tasks"]
        self.assertEqual(after_tasks["GKT-ADD"]["status"], "open")
        self.assertEqual(after_tasks["GKT-RESOLVED"]["status"], "resolved")
        self.assertNotIn("current_verification", after_tasks["GKT-ADD"])


class CuratorRelationshipProposalQueuePageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.item = {
            "task_id": "GKT-QUEUE", "task_status": "open", "command_id": "sample-command",
            "command_title": "Sample Command", "article_id": "sample-article",
            "article_title": "Sample Article", "outcome": "add_reciprocal",
            "rationale": "Specific diagnostic purposes align.",
            "command_declares_article": True, "article_declares_command": False,
            "metadata_change": "Add 'sample-command' to related_commands.",
            "affected_record": "/repo/published/sample-article.json", "change_applied": False,
        }
        self.queue = {
            "items": [self.item], "actionable_count": 1, "visible_count": 1, "closed_count": 2,
            "filters": {"outcome": "", "status": ""},
            "options": {"outcomes": ("add_reciprocal", "remove_unsupported", "human_review_required"),
                        "statuses": ("open", "in_progress", "deferred")},
        }

    def test_page_renders_proposal_fields_filters_task_link_and_navigation(self):
        with patch("app.app.CuratorRelationshipProposalQueueService") as service:
            service.return_value.queue.return_value = self.queue
            with patch("app.services.curator_targeted_verification_service.CuratorTargetedVerificationService.verify") as verify:
                response = self.client.get("/curator/relationship-proposals?outcome=add_reciprocal")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Relationship Proposal Queue", html)
        self.assertIn("Add &#39;sample-command&#39; to related_commands.", html)
        self.assertIn("/repo/published/sample-article.json", html)
        self.assertIn("Proposal only", html)
        self.assertIn('href="/curator/tasks/GKT-QUEUE?', html)
        self.assertIn("Review task", html)
        self.assertIn('aria-current="page">Relationship Proposals', html)
        self.assertIn('class="row g-3"', html)
        verify.assert_not_called()
        service.return_value.queue.assert_called_once_with(outcome="add_reciprocal", status="")

    def test_zero_state_and_filtered_zero_state_are_distinct(self):
        for actionable, expected in (
            (0, "No relationship repair proposals currently require review."),
            (2, "No relationship repair proposals match the selected filters."),
        ):
            queue = {**self.queue, "items": [], "visible_count": 0, "actionable_count": actionable}
            with self.subTest(actionable=actionable), patch(
                    "app.app.CuratorRelationshipProposalQueueService") as service:
                service.return_value.queue.return_value = queue
                html = self.client.get("/curator/relationship-proposals").get_data(as_text=True)
                self.assertIn(expected, html)

    def test_queue_return_context_is_preserved_on_task_detail(self):
        task = {
            "task_id": "GKT-QUEUE", "status": "open", "title": "Relationship task",
            "finding_type": "article_command_reciprocity_conflict", "content_type": "command",
            "content_identifier": "sample-command", "evidence": [],
        }
        with patch("app.app.CuratorRelationshipProposalQueueService") as queue_service, patch("app.app.CuratorTaskService.get", return_value={
                **task, "classification": "Defect", "priority": "Medium", "owner": "Curator",
                "knowledge_debt_score": 1, "confidence": "high", "explanation": "Review.",
                "navigation": {"url": "/commands/sample-command", "label": "Open affected command"},
                "guidance": {"why": "Review.", "impact": "Mismatch.", "certainty": "Human decision."},
                "recommended_action": "Review.", "original_evidence": [], "current_content": None,
                "current_relationship_evidence": None, "relationship_repair_proposal": None,
                "current_verification": None, "history": [], "related_workflows": [],
                "related_articles": [], "related_commands": [], "related_scripts": [],
                "related_tasks": [], "live_related_knowledge": {"articles": []},
                "future_automated_fix": False, "affected_fingerprint": "",
            }), patch("app.app.CuratorResolutionService.get", return_value=None), patch(
                "app.app.CuratorConfusingStepImprovementService.get", return_value=None):
            queue_service.return_value.queue.return_value = {
                **self.queue,
                "filters": {"outcome": "add_reciprocal", "status": "open"},
            }
            queue_html = self.client.get(
                "/curator/relationship-proposals?outcome=add_reciprocal&status=open"
            ).get_data(as_text=True)
            href = html_module.unescape(re.search(
                r'href="([^"]+)"[^>]*>Review task</a>', queue_html
            ).group(1))
            html = self.client.get(href).get_data(as_text=True)
        self.assertIn("Return to Relationship Proposals", html)
        self.assertIn(
            'href="/curator/relationship-proposals?outcome=add_reciprocal&amp;status=open"', html
        )
        self.assertNotIn("Maintenance session context", html)


if __name__ == "__main__":
    unittest.main()
