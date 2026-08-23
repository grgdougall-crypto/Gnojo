import copy
import json
import tempfile
import unittest
from pathlib import Path

from flask import render_template

from app.app import app
from app.services.curator_relationship_repair_proposal_service import (
    CuratorRelationshipRepairProposalService,
)
from app.services.curator_task_service import CuratorTaskService
from curator.memory import CuratorMemoryStore


class CuratorRelationshipRepairProposalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CuratorRelationshipRepairProposalService()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def task(finding_type="article_command_reciprocity_conflict"):
        return {
            "task_id": "GKT-REL", "status": "open", "title": "Relationship task",
            "finding_type": finding_type, "content_type": "command",
            "content_identifier": "sample-command", "evidence": ["Original immutable evidence"],
            "history": [{"event": "observed", "evidence": ["Original immutable evidence"]}],
        }

    @staticmethod
    def relationship(command_summary, article_overview, *, command_declares=True,
                     article_declares=False, structured=None):
        command_id, article_id = "sample-command", "sample-article"
        return {
            "heading": "Current relationship declarations", "target_found": True, "affected_id": command_id,
            "source_path": "/repo/commands/sample-command.json",
            "related_articles": [article_id] if command_declares else [],
            "related_commands": [],
            "command_context": {"id": command_id, "title": "Sample command",
                                "name": "sample-command", "summary": command_summary},
            "articles": [{"id": article_id, "found": True, "title": "Sample article",
                          "overview": article_overview, "category": "Diagnostics", "tags": [],
                          "structured_commands": structured or [],
                          "related_commands": [command_id] if article_declares else [],
                          "source_path": "/repo/published/sample-article.json"}],
        }

    def test_add_reciprocal_uses_specific_shared_diagnostic_purpose(self):
        relationship = self.relationship(
            "Lists physical adapters, link status, and link speed.",
            "Check whether a wired Ethernet cable has link lights and a physical connection.",
        )
        result = self.service.build(self.task(), relationship)
        self.assertEqual(result["outcome"], "add_reciprocal")
        self.assertEqual(result["metadata_change"], "Add 'sample-command' to related_commands.")
        self.assertEqual(result["affected_record"], "/repo/published/sample-article.json")
        self.assertFalse(result["change_applied"])

    def test_remove_unsupported_distinguishes_ip_reachability_from_link_state(self):
        relationship = self.relationship(
            "Tests whether a remote host can be reached with ICMP echo requests and reports response time.",
            "Check whether an Ethernet cable has a physical connection and visible link lights.",
        )
        result = self.service.build(self.task(), relationship)
        self.assertEqual(result["outcome"], "remove_unsupported")
        self.assertEqual(result["metadata_change"], "Remove 'sample-article' from related_articles.")
        self.assertEqual(result["affected_record"], "/repo/commands/sample-command.json")

    def test_remove_unsupported_distinguishes_filesystem_integrity_from_capacity(self):
        relationship = self.relationship(
            "Runs an NTFS scan to detect logical file-system errors.",
            "Diagnose low storage, remaining space, temporary files, and safe cleanup recommendations.",
        )
        self.assertEqual(self.service.build(self.task(), relationship)["outcome"], "remove_unsupported")

    def test_ambiguous_missing_multiple_and_aligned_evidence_require_human_review(self):
        ambiguous = self.relationship("Shows diagnostic details.", "General troubleshooting guidance.")
        self.assertEqual(self.service.build(self.task(), ambiguous)["outcome"], "human_review_required")
        missing = copy.deepcopy(ambiguous)
        missing["articles"][0]["found"] = False
        self.assertEqual(self.service.build(self.task(), missing)["outcome"], "human_review_required")
        multiple = copy.deepcopy(ambiguous)
        multiple["articles"].append({**multiple["articles"][0], "id": "another"})
        self.assertEqual(self.service.build(self.task(), multiple)["outcome"], "human_review_required")
        aligned = self.relationship("Lists running processes and memory usage.",
                                    "Task Manager shows Windows processes using memory.",
                                    article_declares=True)
        self.assertEqual(self.service.build(self.task(), aligned)["outcome"], "human_review_required")

    def test_opposite_one_sided_declaration_targets_command_record(self):
        relationship = self.relationship(
            "Shows active IP addresses and default gateway configuration.",
            "Inspect TCP/IP configuration including IP address and default gateway.",
            command_declares=False, article_declares=True,
        )
        result = self.service.build(self.task(), relationship)
        self.assertEqual(result["outcome"], "add_reciprocal")
        self.assertEqual(result["metadata_change"], "Add 'sample-article' to related_articles.")
        self.assertEqual(result["affected_record"], "/repo/commands/sample-command.json")

    def test_unrelated_task_family_gets_no_proposal(self):
        relationship = self.relationship("Lists running processes.", "Task Manager shows processes.")
        self.assertIsNone(self.service.build(self.task("missing_safety_guidance"), relationship))

    def test_task_projection_and_proposal_generation_are_non_mutating(self):
        command_path = self.write("knowledge_base/commands/sample-command.json", {
            "id": "sample-command", "title": "Adapter status", "summary": "Shows adapter link status and link speed.",
            "category": "Networking", "related_articles": ["sample-article"], "related_commands": [],
        })
        article_path = self.write("knowledge_base/published/sample-article.json", {
            "id": "sample-article", "canonical_id": "sample-article", "title": "Ethernet link check",
            "overview": "Check a wired Ethernet physical connection and link lights.",
            "category": "Networking", "related_commands": [], "commands": [],
        })
        task = self.task()
        CuratorMemoryStore(self.root / "curation_memory").save({"tasks": {"GKT-REL": task}})
        before_command, before_article = command_path.read_bytes(), article_path.read_bytes()
        memory_path = self.root / "curation_memory" / "memory.json"
        before_state = memory_path.read_bytes()
        projected = CuratorTaskService(self.root).get("GKT-REL")
        self.assertEqual(projected["relationship_repair_proposal"]["outcome"], "add_reciprocal")
        self.assertEqual(command_path.read_bytes(), before_command)
        self.assertEqual(article_path.read_bytes(), before_article)
        self.assertEqual(memory_path.read_bytes(), before_state)
        self.assertEqual(projected["original_evidence"], ["Original immutable evidence"])
        self.assertIsNone(projected["current_verification"] or None)
        self.assertEqual(projected["status"], "open")

    def test_template_keeps_then_now_proposal_and_verify_distinct(self):
        relationship = self.relationship(
            "Lists running processes and memory usage.",
            "Task Manager shows Windows processes using memory.",
        )
        proposal = self.service.build(self.task(), relationship)
        task = {
            **self.task(), "classification": "Defect", "priority": "Medium", "owner": "Curator",
            "knowledge_debt_score": 1, "confidence": "high", "explanation": "Review it.",
            "navigation": {"url": "/commands/sample-command", "label": "Open affected command"},
            "guidance": {"why": "Review.", "impact": "Mismatch.", "certainty": "Human decision."},
            "recommended_action": "Review.", "original_evidence": ["Original immutable evidence"],
            "current_content": None, "current_relationship_evidence": relationship,
            "relationship_repair_proposal": proposal, "current_verification": None,
            "related_workflows": [], "related_articles": [], "related_commands": [],
            "related_scripts": [], "related_tasks": [], "live_related_knowledge": {"articles": []},
            "history": [], "future_automated_fix": False, "affected_fingerprint": "",
        }
        context = {
            "owners": ["Curator"], "priorities": ["Medium"], "status_kind": "info",
            "status_message": "", "resolution_package": None, "confusing_step_proposal": None,
            "verification_presentation": None,
            "task_review": {"history_count": 0, "recent_history": [], "remaining_history": []},
            "session_task_actionable": False, "return_to": "", "curator_session": "", "category": "all",
        }
        with app.test_request_context():
            html = render_template("curator_task_detail.html", task=task, **context)
        self.assertIn("Original audit evidence", html)
        self.assertIn("Current relationship declarations", html)
        self.assertIn("Relationship Review Context", html)
        self.assertIn("Relationship Repair Proposal", html)
        self.assertIn("no metadata change has been applied.", html)
        self.assertIn("Targeted Verification", html)

    def test_template_has_no_repository_access(self):
        source = (Path(__file__).parents[1] / "app/templates/curator_task_detail.html").read_text(encoding="utf-8")
        self.assertNotIn("Repository", source)
        self.assertNotIn("CuratorInventory", source)


if __name__ == "__main__":
    unittest.main()
