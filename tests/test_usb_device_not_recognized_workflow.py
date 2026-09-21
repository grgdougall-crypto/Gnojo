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


class UsbDeviceNotRecognizedWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(
            "app/decision_trees/usb_device_not_recognized.json"
        )
        cls.workflow = json.loads(cls.workflow_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True, SECRET_KEY="usb-workflow-test")
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
            "usb_device_not_recognized",
            catalog,
            catalog["usb_device_not_recognized"].get("version"),
        )
        quality = WorkflowQualityValidator().validate(engine.workflow, set(catalog))
        self.assertEqual(quality["overall_status"], "CLEAN")
        self.assertEqual(quality["findings"], [])
        self.assertEqual(quality["metrics"]["reachable_nodes"], len(self.workflow["nodes"]))
        self.assertEqual(quality["metrics"]["unreachable_nodes"], 0)
        self.assertEqual(quality["metrics"]["cycles_detected"], 0)
        self.assertTrue(WorkflowProgressService.enabled(engine.workflow))

    def test_observation_precedes_port_test_and_success_requires_function(self):
        pages = self._run([
            "yes", "other", "", "direct", "", "yes", "", "works", "yes",
        ])
        self.assertLess(
            self._first_page(pages, "Inspect the USB Connection"),
            self._first_page(pages, "Reconnect and Test One Compatible Port"),
        )
        self.assertLess(
            self._first_page(pages, "Reconnect and Test One Compatible Port"),
            self._first_page(pages, "Does Windows consistently recognize"),
        )
        self.assertIn("USB Device Function Verified", pages[-1])
        self.assertIn("intended function works", pages[-1])

        recognized_only = self._run([
            "yes", "other", "", "direct", "", "yes", "", "works",
            "recognized_only",
        ])
        self.assertIn("Device Function Still Needs Support", recognized_only[-1])

    def test_hardware_driver_policy_and_intermittent_paths_fail_closed(self):
        damaged = self._run(["yes", "other", "", "damaged"])
        self.assertIn("Stop Using the Damaged Connection", damaged[-1])

        driver = self._run([
            "yes", "other", "", "direct", "", "yes", "", "error", "", "warning",
        ])
        self.assertIn("Driver Review Is Required", driver[-1])
        self.assertIn("Do not uninstall devices broadly", driver[-1])

        policy = self._run(["restricted"])
        self.assertIn("USB Policy Review Is Required", policy[-1])
        self.assertIn("Do not disable USB security controls", policy[-1])

        intermittent = self._run([
            "yes", "other", "", "direct", "", "yes", "", "intermittent",
        ])
        self.assertIn("Intermittent USB Connection Needs Support", intermittent[-1])

    def test_storage_input_and_uncertainty_are_bounded(self):
        storage = self._run(["yes", "storage", "accessible"])
        self.assertIn("Protect the Storage Data First", storage[-1])
        self.assertIn("Do not repeatedly reconnect", storage[-1])

        only_input = self._run(["yes", "input", "no"])
        self.assertIn("Arrange an Alternate Input Method", only_input[-1])

        uncertain = self._run(["yes", "other", "", "unclear"])
        self.assertIn("USB Result Could Not Be Verified", uncertain[-1])

    def test_specialized_printer_and_audio_workflows_are_reused(self):
        printer = self._run(["yes", "printer"])
        self.assertIn("Continue to Printer Not Working", printer[-1])
        audio = self._run(["yes", "audio"])
        self.assertIn("Continue to No Sound", audio[-1])

        self.assertEqual(
            self.workflow["nodes"]["printer_handoff"]["next_workflow"],
            "printer",
        )
        self.assertEqual(
            self.workflow["nodes"]["audio_handoff"]["next_workflow"],
            "no_sound",
        )

    def test_device_manager_article_is_the_only_linked_knowledge_identity(self):
        linked = {
            node["knowledge_article"]
            for node in self.workflow["nodes"].values()
            if node.get("knowledge_article")
        }
        self.assertEqual(linked, {"using-device-manager-to-troubleshoot-hardware"})
        article_path = Path("knowledge_base/published") / (
            "using-device-manager-to-troubleshoot-hardware.json"
        )
        article = json.loads(article_path.read_text(encoding="utf-8"))
        self.assertEqual(
            article.get("canonical_id", article["id"]),
            "using-device-manager-to-troubleshoot-hardware",
        )

    def test_builtin_is_public_without_creating_a_publication(self):
        publication_root = Path(self.temporary.name) / "publications"
        service = WorkflowCatalogService(
            publications=WorkflowPublicationService(publication_root)
        )
        entry = service.catalog()["usb_device_not_recognized"]
        self.assertEqual(entry["source"], "built_in")
        self.assertIsNone(entry["version"])
        self.assertEqual(entry["discovery_role"], "public_entry")
        self.assertIn("usb_device_not_recognized", service.discovery_catalog())
        self.assertFalse(publication_root.exists())

    def test_browse_wizard_search_and_studio_surface_the_workflow(self):
        browse = self.client.get("/workflows")
        wizard = self.client.get("/wizard?workflow=usb_device_not_recognized")
        search = self.client.get(
            "/search?q=USB+Device+Not+Recognized&type=workflow"
        )
        studio = self.client.get("/workflow-studio")

        for response in (browse, wizard, search, studio):
            self.assertEqual(response.status_code, 200)
            self.assertIn("USB Device Not Recognized", response.get_data(as_text=True))

        results = [
            result
            for result in SearchService().search_all("USB Device Not Recognized")
            if result.content_type == "Workflow"
        ]
        self.assertTrue(
            any(result.id == "usb_device_not_recognized" for result in results)
        )
        self.assertIn(
            "Is this a Windows device that you can inspect",
            wizard.get_data(as_text=True),
        )

    def test_discovery_gets_do_not_change_workflow_or_publication_storage(self):
        publication_root = Path("app/workflow_publications")
        before_workflow = self.workflow_path.read_bytes()
        before_publications = self._file_snapshot(publication_root)

        for url in (
            "/workflows",
            "/wizard?workflow=usb_device_not_recognized",
            "/search?q=USB+Device+Not+Recognized&type=workflow",
            "/workflow-studio",
        ):
            self.assertEqual(self.client.get(url).status_code, 200)

        self.assertEqual(self.workflow_path.read_bytes(), before_workflow)
        self.assertEqual(self._file_snapshot(publication_root), before_publications)

    def _run(self, answers):
        pages = [
            self.client.get(
                "/wizard?workflow=usb_device_not_recognized&restart=1"
            ).get_data(as_text=True)
        ]
        for answer in answers:
            pages.append(
                self.client.post(
                    "/wizard", data={"answer": answer}, follow_redirects=True
                ).get_data(as_text=True)
            )
        return pages

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
