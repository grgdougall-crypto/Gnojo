import json
import tempfile
import unittest
from pathlib import Path

from app.services.curator_task_service import CuratorTaskService
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from app.services.knowledge_integrity_service import KnowledgeIntegrityService
from curator.checks import CuratorChecks
from curator.inventory import CuratorInventory
from curator.memory import CuratorMemoryStore
from curator.models import AuditFilter
from curator.tasks import KnowledgeTaskService


class CommandRelationshipIntegrityTests(unittest.TestCase):
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

    @staticmethod
    def article(identifier, related_commands=None):
        value = {
            "id": identifier, "canonical_id": identifier, "title": identifier.replace("-", " ").title(),
            "category": "Networking", "overview": "Specific published guidance for deterministic testing.",
            "review": {"status": "approved", "reviewed_by": "Reviewer", "reviewed_at": "2026-08-21T00:00:00+00:00"},
            "sources": [{"title": "Source", "url": "https://example.com/source"}],
        }
        if related_commands is not None:
            value["related_commands"] = related_commands
        return value

    @staticmethod
    def command(identifier, related_articles=None, related_commands=None):
        value = {
            "id": identifier, "title": identifier.replace("-", " ").title(), "category": "Networking",
            "summary": "Specific command guidance for deterministic relationship testing.",
            "syntax": identifier, "examples": [{"command": identifier}], "permissions": {"requires_elevation": False},
            "risk": {"level": "Low"}, "sources": [{"url": "https://example.com/command"}], "tags": ["test"],
            "review_status": "reviewed",
        }
        if related_articles is not None:
            value["related_articles"] = related_articles
        if related_commands is not None:
            value["related_commands"] = related_commands
        return value

    def findings(self):
        inventory = CuratorInventory(self.root).collect()
        return CuratorChecks(self.root).relationship_findings(inventory), inventory

    def test_command_article_valid_missing_inactive_and_malformed_targets(self):
        self.write("knowledge_base/published/active.json", self.article("active"))
        self.write("knowledge_base/archive/inactive.json", self.article("inactive"))
        self.write("knowledge_base/commands/valid.json", self.command("valid", ["active"]))
        self.write("knowledge_base/commands/missing.json", self.command("missing", ["absent"]))
        self.write("knowledge_base/commands/inactive.json", self.command("inactive-command", ["inactive"]))
        self.write("knowledge_base/commands/malformed.json", self.command("malformed", ["Not an ID!"]))

        findings, _ = self.findings()
        invalid = [item for item in findings if item.finding_type == "command_article_relationship_invalid"]
        self.assertEqual({item.content_identifier for item in invalid}, {"missing", "inactive-command", "malformed"})
        self.assertFalse(any(item.content_identifier == "valid" for item in invalid))
        self.assertTrue(any("inactive lifecycle" in item.explanation for item in invalid))

    def test_command_command_valid_missing_and_malformed_targets(self):
        self.write("knowledge_base/commands/target.json", self.command("target"))
        self.write("knowledge_base/commands/valid.json", self.command("valid", related_commands=["target"]))
        self.write("knowledge_base/commands/missing.json", self.command("missing", related_commands=["absent"]))
        self.write("knowledge_base/commands/malformed.json", self.command("malformed", related_commands=[""]))

        findings, _ = self.findings()
        invalid = [item for item in findings if item.finding_type == "command_command_relationship_invalid"]
        self.assertEqual({item.content_identifier for item in invalid}, {"missing", "malformed"})

    def test_reciprocity_agreement_contradiction_and_one_sided_declaration(self):
        self.write("knowledge_base/published/agreed.json", self.article("agreed", ["agreed-command"]))
        self.write("knowledge_base/published/conflict.json", self.article("conflict", ["conflict-command"]))
        self.write("knowledge_base/published/one-sided.json", self.article("one-sided", ["one-sided-command"]))
        self.write("knowledge_base/commands/agreed-command.json", self.command("agreed-command", ["agreed"]))
        self.write("knowledge_base/commands/conflict-command.json", self.command("conflict-command", []))
        self.write("knowledge_base/commands/one-sided-command.json", self.command("one-sided-command"))

        findings, _ = self.findings()
        conflicts = [item for item in findings if item.finding_type == "article_command_reciprocity_conflict"]
        self.assertEqual([item.content_identifier for item in conflicts], ["conflict-command"])

    def test_gkt_identity_context_and_navigation_are_stable(self):
        self.write("knowledge_base/commands/broken.json", self.command("broken", ["absent"]))
        findings, inventory = self.findings()
        finding = next(item for item in findings if item.finding_type == "command_article_relationship_invalid")
        state = {"tasks": {}}
        service = KnowledgeTaskService()
        first = service.reconcile(state, [finding], inventory, run_id="RUN-1",
                                  observed_at="2026-08-21T00:00:00+00:00", filters=AuditFilter(content_type="command"))
        second = service.reconcile(state, [finding], inventory, run_id="RUN-2",
                                   observed_at="2026-08-21T01:00:00+00:00", filters=AuditFilter(content_type="command"))
        self.assertEqual(first["observed"], second["observed"])
        task = state["tasks"][first["observed"][0]]
        self.assertEqual(task["classification"], "Defect")
        self.assertEqual(task["related_commands"], ["broken"])
        self.assertEqual(task["related_articles"], [])
        CuratorMemoryStore(self.root / "curation_memory").save(state)
        presented = CuratorTaskService(self.root).get(task["task_id"])
        self.assertEqual(presented["navigation"]["url"].split("?", 1)[0], "/commands/broken")

    def test_integrity_report_counts_details_and_zero_state(self):
        clean = KnowledgeIntegrityService(self.root).report()
        self.assertEqual(clean["counts"]["command_relationship_defects"], 0)
        self.assertEqual(clean["command_relationship_defects"], [])
        self.assertIn("broken_relationships", clean["counts"])
        self.write("knowledge_base/commands/broken.json", self.command("broken", ["absent"]))
        report = KnowledgeIntegrityService(self.root).report()
        self.assertEqual(report["counts"]["command_relationship_defects"], 1)
        self.assertEqual(report["command_relationship_defects"][0]["content_identifier"], "broken")

    def test_targeted_verification_is_read_only_and_does_not_resolve_task(self):
        command_path = self.write("knowledge_base/commands/broken.json", self.command("broken", ["absent"]))
        findings, inventory = self.findings()
        finding = next(item for item in findings if item.finding_type == "command_article_relationship_invalid")
        state = {"tasks": {}}
        result = KnowledgeTaskService().reconcile(
            state, [finding], inventory, run_id="RUN-1", observed_at="2026-08-21T00:00:00+00:00",
            filters=AuditFilter(content_type="command"))
        task_id = result["observed"][0]
        store = CuratorMemoryStore(self.root / "curation_memory")
        store.save(state)
        before = command_path.read_bytes()
        first = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(first["status"], "still_detected")
        self.assertEqual(command_path.read_bytes(), before)
        self.write("knowledge_base/published/active.json", self.article("active"))
        command_path.write_text(json.dumps(self.command("broken", ["active"])), encoding="utf-8")
        corrected = command_path.read_bytes()
        second = CuratorTargetedVerificationService(self.root).verify(task_id)
        self.assertEqual(second["status"], "appears_corrected")
        self.assertEqual(command_path.read_bytes(), corrected)
        self.assertEqual(store.load()["tasks"][task_id]["status"], "open")

    def test_unsupported_command_heuristic_remains_not_automatable(self):
        state = {"tasks": {"GKT-HEURISTIC": {
            "task_id": "GKT-HEURISTIC", "status": "open", "content_type": "command",
            "content_identifier": "sample", "curator_rule": "CUR-EDITOR-SCRIPT-001",
            "finding_type": "script_asset_candidate",
        }}}
        CuratorMemoryStore(self.root / "curation_memory").save(state)
        result = CuratorTargetedVerificationService(self.root).verify("GKT-HEURISTIC")
        self.assertEqual(result["status"], "not_automatable")


if __name__ == "__main__":
    unittest.main()
