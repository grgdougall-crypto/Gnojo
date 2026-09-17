import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows, load_runtime_workflow
from app.engine.decision_engine import DecisionEngine
from app.services.search_service import SearchService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_catalog_service import WorkflowCatalogService
from app.services.workflow_progress_service import WorkflowProgressService
from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_quality_validator import WorkflowQualityValidator
from app.services.workflow_validation_service import WorkflowValidationService


class WindowsUpdateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path("app/decision_trees/windows_update.json")
        cls.workflow = json.loads(cls.workflow_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="windows-update-test")
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_workflow_is_clean_reachable_bounded_and_branch_aware(self):
        validation = WorkflowValidationService().validate(self.workflow)
        self.assertTrue(validation["is_valid"])
        self.assertEqual(validation["errors"], [])
        self.assertEqual(validation["warnings"], [])

        catalog = available_workflows()
        engine = DecisionEngine()
        load_runtime_workflow(
            engine,
            "windows_update",
            catalog,
            catalog["windows_update"].get("version"),
        )
        quality = WorkflowQualityValidator().validate(engine.workflow, set(catalog))
        self.assertEqual(quality["overall_status"], "CLEAN")
        self.assertEqual(quality["findings"], [])
        self.assertEqual(quality["metrics"]["reachable_nodes"], len(self.workflow["nodes"]))
        self.assertEqual(quality["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(quality["metrics"]["cycles_detected"], 0)
        self.assertGreaterEqual(quality["metrics"]["terminal_nodes"], 5)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))

    def test_stalled_update_observes_before_mutation_and_verifies_success(self):
        pages = self._run([
            "yes", "", "stuck", "", "unchanged", "", "no", "no", "",
            "progressing", "", "complete",
        ])
        self.assertLess(
            self._first_page(pages, "Observe One Update Interval"),
            self._first_page(pages, "Inspect Available Storage"),
        )
        self.assertLess(
            self._first_page(pages, "Inspect Available Storage"),
            self._first_page(pages, "Complete One Controlled Restart"),
        )
        self.assertLess(
            self._first_page(pages, "Complete One Controlled Restart"),
            self._first_page(pages, "Verify the Update Result"),
        )
        self.assertIn("Windows Update Verified", pages[-1])
        self.assertIn("no blocking update error", pages[-1])

    def test_restart_loop_and_uncertainty_fail_closed(self):
        restart_loop = self._run(["yes", "", "restart", "yes"])
        self.assertIn("Persistent Update Failure Needs Support", restart_loop[-1])
        self.assertIn("Do not delete system folders", restart_loop[-1])

        uncertain = self._run(["yes", "", "unsure", "", "unclear"])
        self.assertIn("Update State Could Not Be Verified", uncertain[-1])
        self.assertIn("rather than guessing", uncertain[-1])

    def test_storage_blocker_uses_existing_safe_handoff(self):
        pages = self._run(["yes", "", "failed", "", "storage", "", "yes"])
        self.assertIn("Continue to Low Disk Space", pages[-1])
        self.assertIn("windows-storage-performance", self._knowledge_links())
        node = self.workflow["nodes"]["low_storage_handoff"]
        self.assertEqual(node["type"], "transition")
        self.assertEqual(node["next_workflow"], "low_storage")

    def test_policy_service_and_servicing_cases_escalate_without_broad_repair(self):
        policy = self._run(["yes", "", "managed"])
        self.assertIn("Managed Update Support Required", policy[-1])
        self.assertIn("Do not disable security controls", policy[-1])

        servicing = self._run(["yes", "", "failed", "", "servicing"])
        self.assertIn("Windows Servicing Review Required", servicing[-1])
        self.assertIn("are not run automatically", servicing[-1])

    def test_linked_knowledge_identities_are_published_and_canonical(self):
        linked = self._knowledge_links()
        self.assertEqual(
            linked,
            {"windows-slow-check-updates", "windows-storage-performance"},
        )
        for article_id in linked:
            article_path = Path("knowledge_base/published") / f"{article_id}.json"
            article = json.loads(article_path.read_text(encoding="utf-8"))
            self.assertEqual(article.get("canonical_id", article["id"]), article_id)

    def test_builtin_is_publicly_discoverable_without_creating_a_publication(self):
        publication_root = Path(self.temporary.name) / "publications"
        service = WorkflowCatalogService(
            publications=WorkflowPublicationService(publication_root)
        )
        entry = service.catalog()["windows_update"]
        self.assertEqual(entry["source"], "built_in")
        self.assertIsNone(entry["version"])
        self.assertEqual(entry["discovery_role"], "public_entry")
        self.assertIn("windows_update", service.discovery_catalog())
        self.assertFalse(publication_root.exists())

    def test_public_browse_wizard_search_and_studio_surface_the_workflow(self):
        browse = self.client.get("/workflows")
        wizard = self.client.get("/wizard?workflow=windows_update")
        search = self.client.get("/search?q=Windows+Update+Issue&type=workflow")
        studio = self.client.get("/workflow-studio")

        for response in (browse, wizard, search, studio):
            self.assertEqual(response.status_code, 200)
            self.assertIn("Windows Update Issue", response.get_data(as_text=True))

        results = [
            result for result in SearchService().search_all("Windows Update Issue")
            if result.content_type == "Workflow"
        ]
        self.assertTrue(any(result.id == "windows_update" for result in results))
        self.assertIn(
            "Is this a Windows device that you can inspect",
            wizard.get_data(as_text=True),
        )
        first_step = self.client.post(
            "/wizard", data={"answer": "yes"}, follow_redirects=True
        )
        self.assertIn("Inspect the Current Update State", first_step.get_data(as_text=True))

    def test_discovery_gets_do_not_change_workflow_or_publication_storage(self):
        publication_root = Path("app/workflow_publications")
        before_workflow = self.workflow_path.read_bytes()
        before_publications = self._file_snapshot(publication_root)

        for url in (
            "/workflows",
            "/wizard?workflow=windows_update",
            "/search?q=Windows+Update+Issue&type=workflow",
            "/workflow-studio",
        ):
            self.assertEqual(self.client.get(url).status_code, 200)

        self.assertEqual(self.workflow_path.read_bytes(), before_workflow)
        self.assertEqual(self._file_snapshot(publication_root), before_publications)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=windows_update&restart=1"
            ).get_data(as_text=True)
        ]
        for answer in answers:
            pages.append(
                self.client.post(
                    "/wizard", data={"answer": answer}, follow_redirects=True
                ).get_data(as_text=True)
            )
        return pages

    def _knowledge_links(self):
        return {
            node["knowledge_article"]
            for node in self.workflow["nodes"].values()
            if node.get("knowledge_article")
        }

    def _first_page(self, pages, text):
        for index, page in enumerate(pages):
            if text in page:
                return index
        self.fail(f"Expected workflow page containing {text!r}.")

    @staticmethod
    def _file_snapshot(root):
        if not root.exists():
            return {}
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
