import json
import unittest
from pathlib import Path

from app.knowledge.article_validator import ArticleValidator
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.script_repository import ScriptRepository
from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_validation_service import WorkflowValidationService


ROOT = Path(__file__).resolve().parents[1]
ARTICLE_ID = "how-to-inspect-and-clear-a-stuck-windows-print-queue"


class PrinterQueueCompletionTests(unittest.TestCase):
    def setUp(self):
        self.workflow_path = ROOT / "app" / "decision_trees" / "printer.json"
        self.article_path = ROOT / "knowledge_base" / "published" / f"{ARTICLE_ID}.json"
        self.workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        self.article = json.loads(self.article_path.read_text(encoding="utf-8"))
        self.nodes = self.workflow["nodes"]

    def test_published_definition_is_clean_and_existing_printer_paths_remain_available(self):
        validation = WorkflowValidationService().validate(self.workflow)

        self.assertTrue(validation["is_valid"], validation["errors"])
        self.assertEqual(validation["quality"]["overall_status"], "CLEAN")
        self.assertEqual(validation["unreachable_nodes"], [])
        self.assertEqual(
            self.nodes["identify_printing_problem"]["answers"]["other"]["next"],
            "check_printer_status",
        )
        self.assertEqual(
            self.nodes["inspect_printer_display"]["knowledge_article"],
            "printer-inspect-printer-display",
        )
        self.assertEqual(
            self.nodes["clear_printer_warning"]["knowledge_article"],
            "how-to-clear-the-printer-warning",
        )

    def test_stuck_job_path_inspects_before_mutation_and_verifies_success(self):
        route = [
            "identify_printing_problem",
            "inspect_print_queue",
            "verify_printer_available",
            "verify_stuck_jobs",
            "cancel_one_stuck_job",
            "verify_cancelled_job",
            "authorize_queue_clear",
            "clear_stuck_queue",
            "verify_queue_cleared",
            "test_queue_printing",
            "verify_queue_printing",
            "resolved_queue",
        ]

        self.assertEqual(
            self.nodes["identify_printing_problem"]["answers"]["stuck_job"]["label"],
            "A print job is stuck",
        )
        for earlier, later in zip(route, route[1:]):
            node = self.nodes[earlier]
            destinations = {
                value["next"] for value in node.get("answers", {}).values()
                if isinstance(value, dict) and value.get("next")
            }
            if node.get("next"):
                destinations.add(node["next"])
            self.assertIn(later, destinations, f"{earlier} does not lead to {later}")

        self.assertLess(route.index("inspect_print_queue"), route.index("cancel_one_stuck_job"))
        self.assertLess(route.index("cancel_one_stuck_job"), route.index("verify_cancelled_job"))
        self.assertLess(route.index("clear_stuck_queue"), route.index("verify_queue_cleared"))
        self.assertLess(route.index("test_queue_printing"), route.index("verify_queue_printing"))
        self.assertIn(
            "leave the queue and print successfully",
            self.nodes["verify_queue_printing"]["question"],
        )

    def test_stuck_job_uncertainty_and_failed_verification_end_safely(self):
        self.assertEqual(
            self.nodes["verify_printer_available"]["answers"]["unknown"]["next"],
            "queue_support_needed",
        )
        self.assertEqual(
            self.nodes["authorize_queue_clear"]["answers"]["no"]["next"],
            "queue_support_needed",
        )
        self.assertEqual(
            self.nodes["verify_queue_cleared"]["answers"]["no"]["next"],
            "queue_support_needed",
        )
        self.assertEqual(
            self.nodes["verify_queue_printing"]["answers"]["no"]["next"],
            "queue_support_needed",
        )
        message = self.nodes["queue_support_needed"]["message"]
        self.assertIn("Stop making queue changes", message)
        self.assertIn("Do not remove other users' jobs", message)
        self.assertIn("Do not", self.nodes["clear_stuck_queue"]["instruction"])
        self.assertIn("WhatIf", self.nodes["clear_stuck_queue"]["instruction"])

    def test_new_article_is_valid_and_linked_canonically(self):
        self.assertEqual(ArticleValidator.validate(self.article), [])
        self.assertEqual(self.article["id"], ARTICLE_ID)
        self.assertEqual(self.article["canonical_id"], ARTICLE_ID)
        self.assertEqual(self.article["review"]["status"], "approved")
        self.assertEqual(
            self.article["related_commands"],
            ["get-printer", "get-printjob", "get-service"],
        )
        linked_nodes = {
            node_id for node_id, node in self.nodes.items()
            if node.get("knowledge_article") == ARTICLE_ID
        }
        self.assertEqual(
            linked_nodes,
            {
                "inspect_print_queue",
                "cancel_one_stuck_job",
                "clear_stuck_queue",
                "test_queue_printing",
            },
        )

    def test_existing_printer_scripts_are_reused_without_new_relationship_fields(self):
        scripts = {
            item["id"]: item
            for item in ScriptRepository(ROOT / "knowledge_base" / "scripts").get_all()
        }

        self.assertIn("printer", scripts["printer-diagnostic-report"]["related_workflows"])
        self.assertIn("printer", scripts["clear-printer-queue"]["related_workflows"])
        self.assertFalse(scripts["printer-diagnostic-report"]["risk"]["changes_system"])
        self.assertTrue(scripts["clear-printer-queue"]["risk"]["changes_system"])
        self.assertNotIn("related_scripts", self.article)
        self.assertTrue(all("related_scripts" not in node for node in self.nodes.values()))

    def test_reading_published_knowledge_is_write_free(self):
        workflow_before = self.workflow_path.read_bytes()
        article_before = self.article_path.read_bytes()

        publication_service = WorkflowPublicationService(
            ROOT / "app" / "workflow_publications"
        )
        loaded_workflow = publication_service.load_current("printer")["workflow"]
        loaded_article = KnowledgeRepository(
            ROOT / "knowledge_base"
        ).get_published_article(ARTICLE_ID)

        self.assertEqual(loaded_workflow["workflow_id"], "printer")
        self.assertEqual(loaded_article["id"], ARTICLE_ID)
        self.assertEqual(self.workflow_path.read_bytes(), workflow_before)
        self.assertEqual(self.article_path.read_bytes(), article_before)

    def test_accepted_v5_publication_matches_the_tracked_bootstrap(self):
        repository = KnowledgeRepository(ROOT / "knowledge_base")
        published_article = repository.get_published_article(ARTICLE_ID)
        self.assertEqual(published_article["canonical_id"], ARTICLE_ID)
        self.assertEqual(published_article["review"]["status"], "approved")

        publication_service = WorkflowPublicationService(
            ROOT / "app" / "workflow_publications"
        )
        published = publication_service.load_current("printer")
        self.assertIsNotNone(published)
        self.assertEqual(published["publication"]["version"], 5)
        self.assertIn("identify_printing_problem", published["workflow"]["nodes"])
        published_definition = {
            key: value for key, value in published["workflow"].items()
            if key not in {"status", "draft_origin"}
        }
        self.assertEqual(
            self.workflow,
            published_definition,
        )
        self.assertEqual(
            publication_service.content_hash(published["workflow"]),
            published["publication"]["content_hash"],
        )


if __name__ == "__main__":
    unittest.main()
