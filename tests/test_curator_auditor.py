import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curator.auditor import CuratorAuditor
from curator.checks import CuratorChecks
from curator.inventory import CuratorInventory
from curator.locking import AuditAlreadyRunningError, AuditLock
from curator.models import AuditFilter, InventoryRecord


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def workflow(**overrides):
    value = {
        "workflow_id": "sample", "name": "Sample", "description": "Specific diagnostic workflow.",
        "category": "Networking", "platform": "Windows", "start_node": "start",
        "nodes": {
            "start": {"type": "instruction", "title": "Inspect Adapter", "instruction": "Record adapter status.", "next": "done"},
            "done": {"type": "resolution", "title": "Inspection Complete", "resolution": "The adapter state was recorded."},
        },
    }
    value.update(overrides)
    return value


class CuratorAuditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_audit_is_read_only_and_repeatable(self):
        source = self.root / "app" / "decision_trees" / "sample.json"
        write_json(source, workflow())
        before = source.read_bytes()
        first, _ = CuratorAuditor(self.root).audit(write=False)
        second, _ = CuratorAuditor(self.root).audit(write=False)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual([item.identifier for item in first.findings], [item.identifier for item in second.findings])

    def test_inventory_filter_and_unreadable_json_finding(self):
        write_json(self.root / "app" / "decision_trees" / "sample.json", workflow())
        bad = self.root / "app" / "workflow_drafts" / "bad.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not-json", encoding="utf-8")
        records = CuratorInventory(self.root).collect(AuditFilter(content_type="workflow", platform="Windows"))
        self.assertEqual([item.identifier for item in records], ["sample"])
        findings, _ = CuratorChecks().run(CuratorInventory(self.root).collect(AuditFilter(content_type="workflow")))
        self.assertTrue(any(item.finding_type == "unreadable_content" and item.content_identifier == "bad" for item in findings))

    def test_existing_workflow_validator_is_reused(self):
        record = InventoryRecord("workflow", "sample", "Sample", "sample.json", "Networking", "Windows", "draft", workflow())
        with patch("curator.checks.WorkflowValidationService.validate", return_value={"errors": ["broken"], "warnings": [], "reachable_nodes": []}) as validate:
            findings, _ = CuratorChecks().run([record])
        validate.assert_called_once()
        self.assertTrue(any(item.rule == "GNOJO-WORKFLOW-VALIDATOR" for item in findings))

    def test_application_source_delimiter_regression_is_reported(self):
        article = InventoryRecord("article", "article", "Article", "article.json", "Networking", "Windows", "draft", {
            "id": "article", "title": "Article", "category": "Networking",
            "overview": "A sufficiently specific overview for testing.",
            "sources": [{"title": "Vendor | Support", "url": "https://example.com/help"}],
        })
        with patch("app.services.article_review_service.ArticleReviewService._sources", return_value=[]):
            findings, _ = CuratorChecks().run([article])
        self.assertTrue(any(item.domain == "application" and item.rule == "CUR-APP-SOURCE-001" for item in findings))

    def test_lock_prevents_overlap_and_releases(self):
        lock_path = self.root / ".curator-audit.lock"
        with AuditLock(lock_path):
            with self.assertRaises(AuditAlreadyRunningError):
                with AuditLock(lock_path):
                    pass
        self.assertFalse(lock_path.exists())

    def test_output_cannot_target_trusted_content_store(self):
        with self.assertRaisesRegex(ValueError, "trusted Gnojo content store"):
            CuratorAuditor(self.root, self.root / "knowledge_base" / "reports")

    def test_full_report_package_and_source_evidence(self):
        write_json(self.root / "app" / "decision_trees" / "sample.json", workflow())
        article = {
            "schema_version": "1.0", "id": "source-test", "title": "Source Test", "category": "Networking",
            "difficulty": "Beginner", "estimated_time": "2 minutes",
            "overview": "Specific guidance used to verify source audit reporting.", "checklist": ["Inspect the result."],
            "common_indicators": ["The result is unexpected."], "commands": [], "related_topics": ["Networking"],
            "quiz": [{"question": "What should be inspected?", "answers": ["The result", "Nothing"], "correct_answer": "The result"}],
            "sources": [{"title": "Broken", "url": "not-a-url"}],
            "generation": {"provider": None, "model": None, "generated_at": None},
            "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None, "notes": []},
        }
        write_json(self.root / "knowledge_base" / "drafts" / "source-test.json", article)
        result, location = CuratorAuditor(self.root).audit()
        expected = {
            "audit_results.json", "inventory.json", "coverage_gaps.json", "workflow_findings.json",
            "source_findings.json", "relationship_findings.json", "application_findings.json",
            "taxonomy_findings.json", "defects.json", "risks.json", "opportunities.json",
            "recommendations.json", "audit_summary.md", "audit.log.jsonl",
            "knowledge_tasks.json", "knowledge_debt.json", "knowledge_health.json",
            "lessons_learned.json", "memory_summary.json",
        }
        self.assertEqual({path.name for path in location.iterdir()}, expected)
        self.assertTrue(any(item.finding_type == "malformed_source" and item.evidence for item in result.findings))
        summary = (location / "audit_summary.md").read_text(encoding="utf-8")
        for heading in ("Critical Defects", "Content Risks", "Knowledge Opportunities", "Editorial Recommendations", "Coverage Improvements", "System Improvements", "Curator Suggestions"):
            self.assertIn(f"## {heading}", summary)

    def test_every_finding_has_knowledge_operations_classification(self):
        record = InventoryRecord("workflow", "sample", "Sample", "sample.json", "Networking", "Windows", "draft", workflow(platform="UnknownOS"))
        findings, _ = CuratorChecks().run([record])
        self.assertTrue(findings)
        self.assertTrue(all(item.classification in {"defect", "risk", "opportunity", "recommendation"} for item in findings))

    def test_duplicate_detection_respects_lifecycle(self):
        draft = InventoryRecord("workflow", "draft-one", "Same Workflow", "draft.json", "Networking", "Windows", "draft", workflow(workflow_id="draft-one"))
        published = InventoryRecord("workflow", "published-one", "Same Workflow", "published.json", "Networking", "Windows", "published", workflow(workflow_id="published-one"))
        findings, _ = CuratorChecks().run([draft, published])
        self.assertFalse(any(item.finding_type == "duplicate_candidate" for item in findings))
        second_draft = InventoryRecord("workflow", "draft-two", "Same Workflow", "draft-two.json", "Networking", "Windows", "draft", workflow(workflow_id="draft-two"))
        findings, _ = CuratorChecks().run([draft, second_draft])
        duplicate = next(item for item in findings if item.finding_type == "duplicate_candidate")
        self.assertEqual(duplicate.classification, "risk")

    def test_graded_safety_requires_proportional_guidance(self):
        risky = workflow(nodes={
            "start": {"type": "instruction", "title": "Restart Windows", "instruction": "Restart Windows now.", "next": "done"},
            "done": {"type": "resolution", "title": "Complete", "resolution": "Restart completed."},
        })
        record = InventoryRecord("workflow", "sample", "Sample", "sample.json", "Networking", "Windows", "draft", risky)
        findings, _ = CuratorChecks().run([record])
        safety = next(item for item in findings if item.finding_type == "missing_safety_guidance")
        self.assertEqual((safety.classification, safety.safety_level), ("risk", 2))
        risky["nodes"]["start"]["help_text"] = "Save active work before restarting Windows."
        findings, _ = CuratorChecks().run([record])
        self.assertFalse(any(item.finding_type == "missing_safety_guidance" for item in findings))

    def test_repeated_instruction_becomes_opportunity(self):
        text = "Open the adapter properties and record the exact status before changing any configuration settings."
        records = []
        for identifier in ("one", "two"):
            raw = workflow(workflow_id=identifier, name=identifier, nodes={
                "start": {"type": "instruction", "title": "Inspect", "instruction": text, "next": "done"},
                "done": {"type": "resolution", "title": "Done", "resolution": "Inspection complete."},
            })
            records.append(InventoryRecord("workflow", identifier, identifier, f"{identifier}.json", "Networking", "Windows", "draft", raw))
        findings, _ = CuratorChecks().run(records)
        reuse = next(item for item in findings if item.finding_type == "reusable_instruction_pattern")
        self.assertEqual(reuse.classification, "opportunity")

    def test_persistent_memory_reuses_tasks_and_learns_from_recurrence(self):
        article = {
            "id": "source-test", "title": "Source Test", "category": "Networking",
            "overview": "Specific guidance for source testing.",
            "sources": [{"title": "Broken", "url": "not-a-url"}],
        }
        write_json(self.root / "knowledge_base" / "drafts" / "source-test.json", article)
        first, _ = CuratorAuditor(self.root).audit()
        second, _ = CuratorAuditor(self.root).audit()
        first_tasks = {task["task_id"]: task for task in first.knowledge_tasks["tasks"]}
        second_tasks = {task["task_id"]: task for task in second.knowledge_tasks["tasks"]}
        self.assertEqual(set(first_tasks), set(second_tasks))
        self.assertTrue(all(second_tasks[key]["times_observed"] == first_tasks[key]["times_observed"] + 1 for key in first_tasks))
        self.assertGreater(second.lessons_learned["recurring_task_count"], 0)
        self.assertEqual(second.memory_summary["audit_number"], 2)

    def test_full_audit_resolves_absent_finding_but_filtered_audit_does_not(self):
        source = self.root / "knowledge_base" / "drafts" / "source-test.json"
        write_json(source, {"id": "source-test", "title": "Source Test", "category": "Networking", "overview": "Specific guidance.", "sources": [{"title": "Broken", "url": "not-a-url"}]})
        first, _ = CuratorAuditor(self.root).audit()
        task_id = next(task["task_id"] for task in first.knowledge_tasks["tasks"] if task["finding_type"] == "malformed_source")
        source.unlink()
        filtered, _ = CuratorAuditor(self.root).audit(AuditFilter(content_type="workflow"))
        filtered_task = next(task for task in filtered.knowledge_tasks["tasks"] if task["task_id"] == task_id)
        self.assertEqual(filtered_task["status"], "open")
        final, _ = CuratorAuditor(self.root).audit()
        final_task = next(task for task in final.knowledge_tasks["tasks"] if task["task_id"] == task_id)
        self.assertEqual(final_task["status"], "resolved")
        self.assertGreaterEqual(final.knowledge_debt["previous_total"], final.knowledge_debt["total"])


if __name__ == "__main__":
    unittest.main()
