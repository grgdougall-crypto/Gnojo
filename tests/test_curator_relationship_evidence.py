import json
import tempfile
import unittest
from pathlib import Path

from flask import render_template

from app.app import app
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService


class CuratorRelationshipEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CuratorTargetedVerificationService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def article(identifier, related_commands):
        return {
            "id": identifier, "canonical_id": identifier, "title": identifier.replace("-", " ").title(),
            "category": "Networking", "overview": "Current authoritative relationship evidence.",
            "tags": ["network", "review"],
            "commands": [{"command": "ipconfig /all", "description": "Stored evidence."}],
            "related_commands": related_commands,
            "review": {"status": "approved", "reviewed_by": "Reviewer", "reviewed_at": "2026-08-21"},
        }

    @staticmethod
    def command(identifier, *, related_articles=None, related_commands=None):
        value = {"id": identifier, "name": "System Information", "title": identifier.title(), "category": "Networking",
                 "summary": "Current authoritative command relationship evidence.",
                 "platforms": ["Windows 11", "Windows Server"], "tags": ["system", "diagnostics"],
                 "review_status": "reviewed"}
        if related_articles is not None:
            value["related_articles"] = related_articles
        if related_commands is not None:
            value["related_commands"] = related_commands
        return value

    @staticmethod
    def task(finding_type, evidence=None):
        return {
            "task_id": "GKT-REL", "status": "open", "title": "Relationship task",
            "finding_type": finding_type, "content_type": "command",
            "content_identifier": "systeminfo", "evidence": evidence or [],
            "current_evidence": evidence or [],
        }

    def test_reciprocity_projection_shows_command_and_multiple_current_articles(self):
        command_path = self.write("knowledge_base/commands/systeminfo.json",
                                  self.command("systeminfo", related_articles=[]))
        self.write("knowledge_base/published/storage.json", self.article("storage", ["systeminfo"]))
        self.write("knowledge_base/published/performance.json", self.article("performance", []))
        task = self.task("article_command_reciprocity_conflict",
                         ["Article: storage", "Article: performance", "Command: systeminfo"])
        before = command_path.read_bytes()

        result = self.service.relationship_evidence(task)

        self.assertEqual(result["affected_id"], "systeminfo")
        self.assertEqual(result["related_articles"], [])
        self.assertEqual([item["id"] for item in result["articles"]], ["performance", "storage"])
        self.assertEqual(result["articles"][0]["related_commands"], [])
        self.assertEqual(result["articles"][1]["related_commands"], ["systeminfo"])
        self.assertEqual(result["command_context"]["summary"], "Current authoritative command relationship evidence.")
        self.assertEqual(result["command_context"]["platforms"], ["Windows 11", "Windows Server"])
        self.assertEqual(result["command_context"]["tags"], ["system", "diagnostics"])
        self.assertEqual(result["articles"][1]["overview"], "Current authoritative relationship evidence.")
        self.assertEqual(result["articles"][1]["tags"], ["network", "review"])
        self.assertEqual(result["articles"][1]["structured_commands"], ["ipconfig /all"])
        self.assertEqual(command_path.read_bytes(), before)

    def test_invalid_article_projection_shows_declared_value_and_missing_target(self):
        self.write("knowledge_base/commands/systeminfo.json",
                   self.command("systeminfo", related_articles=["missing-article"]))
        result = self.service.relationship_evidence(
            self.task("command_article_relationship_invalid"))
        self.assertEqual(result["related_articles"], ["missing-article"])
        self.assertEqual(result["articles"][0]["id"], "missing-article")
        self.assertFalse(result["articles"][0]["found"])
        self.assertEqual(result["articles"][0]["structured_commands"], [])

    def test_invalid_command_projection_shows_declared_value_and_missing_target(self):
        self.write("knowledge_base/commands/systeminfo.json",
                   self.command("systeminfo", related_commands=["missing-command"]))
        result = self.service.relationship_evidence(
            self.task("command_command_relationship_invalid"))
        self.assertEqual(result["related_commands"], ["missing-command"])
        self.assertEqual(result["commands"][0]["found"], False)

    def test_template_renders_relationship_facts_and_neutral_empty_values(self):
        projection = {
            "heading": "Current relationship declarations", "affected_type": "command",
            "affected_id": "systeminfo", "target_found": True,
            "command_context": {"id": "systeminfo", "title": "System Information", "name": "systeminfo",
                                "summary": "Shows current system metadata.", "category": "Diagnostics",
                                "platforms": ["Windows 11"], "tags": []},
            "related_articles": [], "related_commands": [], "articles": [
                {"id": "storage", "found": True, "title": "Storage",
                 "overview": "Storage purpose.", "category": "Storage", "tags": [],
                 "structured_commands": [], "related_commands": [], "related_commands_declared": True},
                {"id": "missing", "found": False, "title": "",
                 "overview": "", "category": "", "tags": [], "structured_commands": [],
                 "related_commands": [], "related_commands_declared": False},
            ], "commands": [],
        }
        html = self._render(projection)
        self.assertIn("Current relationship declarations", html)
        self.assertIn("Current article relationship data", html)
        self.assertGreaterEqual(html.count("None declared"), 3)
        self.assertIn("Target article record not found", html)
        self.assertIn("Relationship Review Context", html)
        self.assertIn("Shows current system metadata", html)
        self.assertIn("Structured command references", html)
        self.assertIn('href="/commands/systeminfo"', html)
        self.assertIn('href="/knowledge/published/storage"', html)
        self.assertNotIn("affected workflow content is not currently available", html)

    def test_missing_metadata_is_presented_neutrally(self):
        self.write("knowledge_base/commands/systeminfo.json", {
            "id": "systeminfo", "title": "Systeminfo", "summary": "", "category": "",
            "related_articles": ["plain-article"], "related_commands": [],
        })
        self.write("knowledge_base/published/plain-article.json", {
            "id": "plain-article", "canonical_id": "plain-article", "title": "Plain Article",
            "overview": "", "category": "", "related_commands": ["systeminfo"], "commands": [],
        })
        projection = self.service.relationship_evidence(
            self.task("article_command_reciprocity_conflict", ["Article: plain-article"]))
        html = self._render(projection)
        self.assertEqual(projection["command_context"]["tags"], [])
        self.assertEqual(projection["articles"][0]["structured_commands"], [])
        self.assertIn("Tags</dt><dd>None", html)
        self.assertIn("Structured command references</dt><dd>None", html)

    def test_generic_task_keeps_existing_workflow_presentation(self):
        html = self._render(None)
        self.assertIn("Current affected content", html)
        self.assertIn("The affected workflow content is not currently available", html)
        self.assertNotIn("Current relationship declarations", html)
        self.assertIsNone(self.service.relationship_evidence({
            "finding_type": "missing_safety_guidance", "content_type": "workflow_node",
            "content_identifier": "flow:step",
        }))

    @staticmethod
    def _render(projection):
        task = {
            "task_id": "GKT-REL", "title": "Relationship task", "explanation": "Review it.",
            "classification": "Defect", "priority": "High", "owner": "Curator", "status": "open",
            "knowledge_debt_score": 1, "confidence": "high",
            "navigation": {"url": "/commands/systeminfo", "label": "Open affected command"},
            "guidance": {"why": "Review.", "impact": "Relationship mismatch.", "certainty": "Deterministic."},
            "recommended_action": "Review.", "original_evidence": [], "current_content": None,
            "current_relationship_evidence": projection, "history": [], "related_workflows": [],
            "related_articles": [], "related_commands": ["systeminfo"], "related_scripts": [],
            "related_tasks": [], "live_related_knowledge": {"articles": []},
            "finding_type": "article_command_reciprocity_conflict", "future_automated_fix": False,
            "affected_fingerprint": "", "current_verification": None,
        }
        context = {
            "owners": ["Curator"], "priorities": ["High"], "status_kind": "info",
            "status_message": "", "resolution_package": None, "confusing_step_proposal": None,
            "verification_presentation": None,
            "task_review": {"history_count": 0, "recent_history": [], "remaining_history": []},
            "session_task_actionable": False, "return_to": "", "curator_session": "", "category": "all",
        }
        with app.test_request_context():
            return render_template("curator_task_detail.html", task=task, **context)


if __name__ == "__main__":
    unittest.main()
