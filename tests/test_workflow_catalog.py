import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import AVAILABLE_WORKFLOWS, app, available_workflows
from app.services.search_service import SearchService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_catalog_service import (
    WorkflowCatalogError,
    WorkflowCatalogService,
)
from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_draft_service import WorkflowDraftService


class WorkflowCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.history_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.history
        )
        self.history_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.history_patch.stop()
        self.temporary.cleanup()

    def test_home_is_capped_and_links_to_complete_catalog(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Recommended Workflows", html)
        self.assertIn("Browse all workflows", html)
        self.assertIn('href="/workflows"', html)
        self.assertLessEqual(html.count("workflow-card-item"), 4)
        self.assertNotIn('id="workflowCategoryFilters"', html)

    def test_catalog_contains_all_workflows_and_filter_controls(self):
        html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn("Browse Workflows", html)
        self.assertIn('id="workflowCategoryFilters"', html)
        self.assertIn('id="workflowFilterSearch"', html)
        self.assertIn('data-workflow-category="favorites"', html)
        self.assertIn('data-workflow-category="recent"', html)
        self.assertIn("workflow_favorites.js", html)
        for title in (
            "Computer Running Slowly",
            "Internet Not Working",
            "Printer Not Working",
        ):
            self.assertIn(title, html)
        self.assertNotIn("Advanced Network Diagnostics", html)
        self.assertNotIn("Higher-Layer Connectivity Diagnostics", html)

    def test_recent_workflow_is_prioritized_on_home(self):
        record = self.history.start("printer", "Printer Not Working", "start")
        html = self.client.get("/").get_data(as_text=True)
        printer_position = html.index("Printer Not Working")
        application_position = html.index("Application Crashing or Freezing")
        self.assertLess(printer_position, application_position)
        self.history.delete(record["id"])

    def test_favorite_toggle_persists_and_prioritizes_home(self):
        added = self.client.post("/api/workflow-favorites/printer")
        self.assertEqual(added.status_code, 200)
        self.assertTrue(added.get_json()["favorite"])
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session["favorite_workflow_ids"], ["printer"])
        catalog = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn('data-workflow-id="printer"', catalog)
        self.assertIn('aria-label="Remove Printer Not Working from favorites"', catalog)
        home = self.client.get("/").get_data(as_text=True)
        self.assertLess(
            home.index("Printer Not Working"),
            home.index("Application Crashing or Freezing"),
        )
        removed = self.client.post("/api/workflow-favorites/printer")
        self.assertFalse(removed.get_json()["favorite"])

    def test_unknown_favorite_is_rejected(self):
        response = self.client.post("/api/workflow-favorites/not-real")
        self.assertEqual(response.status_code, 404)

    @staticmethod
    def _workflow(workflow_id, name):
        return {
            "workflow_id": workflow_id,
            "name": name,
            "description": f"Current published guidance for {name}.",
            "start_node": "start",
            "nodes": {
                "start": {
                    "type": "instruction", "title": "Start",
                    "instruction": "Inspect the current condition.", "next": "done",
                },
                "done": {"type": "resolution", "title": "Done", "message": "Complete."},
            },
        }

    @classmethod
    def _write_builtin(cls, directory, workflow_id, name=None, filename=None):
        directory.mkdir(parents=True, exist_ok=True)
        workflow = cls._workflow(workflow_id, name or workflow_id.replace("_", " ").title())
        path = directory / (filename or f"{workflow_id}.json")
        path.write_text(json.dumps(workflow), encoding="utf-8")
        return path

    def test_authoritative_catalog_combines_builtin_only_and_partial_publications(self):
        root = Path(self.temporary.name)
        built_ins = root / "built-ins"
        publications = WorkflowPublicationService(root / "publications")
        self._write_builtin(built_ins, "built_in_only", "Built In Only")
        publications.publish(
            self._workflow("published_only", "Published Only"),
            "published_only.json",
        )

        catalog = WorkflowCatalogService(
            built_ins, publications=publications
        ).catalog()

        self.assertEqual(list(catalog), ["built_in_only", "published_only"])
        self.assertEqual(catalog["built_in_only"]["source"], "built_in")
        self.assertEqual(catalog["published_only"]["source"], "published")
        self.assertEqual(catalog["published_only"]["version"], 1)

    def test_active_publication_overrides_builtin_once_and_drafts_are_not_public(self):
        root = Path(self.temporary.name)
        built_ins = root / "built-ins"
        drafts = root / "workflow-drafts"
        publications = WorkflowPublicationService(root / "publications")
        self._write_builtin(built_ins, "shared", "Tracked Shared")
        self._write_builtin(drafts, "draft_only", "Draft Only")
        publications.publish(
            self._workflow("shared", "Current Shared"), "shared.json"
        )

        catalog = WorkflowCatalogService(
            built_ins, publications=publications
        ).catalog()

        self.assertEqual(list(catalog), ["shared"])
        self.assertEqual(catalog["shared"]["name"], "Current Shared")
        self.assertEqual(catalog["shared"]["source"], "published")
        self.assertNotIn("draft_only", catalog)

    def test_conflicting_tracked_identity_fails_closed(self):
        root = Path(self.temporary.name)
        built_ins = root / "built-ins"
        self._write_builtin(
            built_ins,
            "canonical_identity",
            filename="different_filename.json",
        )

        with self.assertRaisesRegex(
            WorkflowCatalogError, "identity does not match filename"
        ):
            WorkflowCatalogService(
                built_ins,
                publications=WorkflowPublicationService(root / "publications"),
            ).catalog()

    def test_catalog_order_is_deterministic_by_title_then_identity(self):
        root = Path(self.temporary.name)
        built_ins = root / "built-ins"
        self._write_builtin(built_ins, "zeta", "Same Title")
        self._write_builtin(built_ins, "alpha", "Same Title")
        self._write_builtin(built_ins, "middle", "A First Title")
        service = WorkflowCatalogService(
            built_ins,
            publications=WorkflowPublicationService(root / "publications"),
        )

        self.assertEqual(list(service.catalog()), ["middle", "alpha", "zeta"])
        self.assertEqual(list(service.catalog()), list(service.catalog()))

    def test_search_uses_builtin_catalog_when_no_publication_exists(self):
        root = Path(self.temporary.name)
        service = SearchService()
        service.knowledge.get_published = lambda: []
        service.commands.get_all = lambda: []
        publications = WorkflowPublicationService(root / "publications")

        with patch(
            "app.services.search_service.WorkflowPublicationService",
            return_value=publications,
        ):
            results = service.search_all("Internet Not Working")

        self.assertTrue(any(
            result.id == "internet" and result.content_type == "Workflow"
            for result in results
        ))

    def test_current_printer_publication_remains_the_selected_runtime_entry(self):
        root = Path(self.temporary.name)
        publications = WorkflowPublicationService(root / "publications")
        publications.publish(
            self._workflow("printer", "Current Printer Publication"),
            "printer.json",
        )

        catalog = WorkflowCatalogService(
            publications=publications,
            built_in_metadata=AVAILABLE_WORKFLOWS,
        ).catalog()

        self.assertEqual(catalog["printer"]["name"], "Printer Not Working")
        self.assertEqual(catalog["printer"]["source"], "published")
        self.assertEqual(catalog["printer"]["version"], 1)
        self.assertEqual(list(catalog).count("printer"), 1)

    def test_networking_discovery_roles_filter_only_public_surfaces(self):
        root = Path(self.temporary.name)
        publications = WorkflowPublicationService(root / "publications")
        publications.publish(self._workflow("vpn", "Vpn"), "vpn.json")
        publications.publish(
            self._workflow(
                "vpn_connectivity_win", "VPN Connectivity Troubleshooting (Windows)"
            ),
            "vpn_connectivity_win.json",
        )
        service = WorkflowCatalogService(
            publications=publications,
            built_in_metadata=AVAILABLE_WORKFLOWS,
        )

        complete = service.catalog()
        discovery = service.discovery_catalog()

        self.assertEqual(complete["internet"]["discovery_role"], "public_entry")
        self.assertEqual(
            complete["vpn_connectivity_win"]["discovery_role"], "public_entry"
        )
        self.assertEqual(
            complete["network_diagnostics"]["discovery_role"], "contextual"
        )
        self.assertEqual(
            complete["higher_layer_connectivity"]["discovery_role"], "contextual"
        )
        self.assertEqual(complete["vpn"]["discovery_role"], "legacy")
        self.assertIn("internet", discovery)
        self.assertIn("vpn_connectivity_win", discovery)
        self.assertNotIn("network_diagnostics", discovery)
        self.assertNotIn("higher_layer_connectivity", discovery)
        self.assertNotIn("vpn", discovery)

    def test_home_and_browse_hide_contextual_and_legacy_networking_entries(self):
        root = Path(self.temporary.name)
        publications = WorkflowPublicationService(root / "publications")
        publications.publish(self._workflow("vpn", "Vpn"), "vpn.json")
        publications.publish(
            self._workflow(
                "vpn_connectivity_win", "VPN Connectivity Troubleshooting (Windows)"
            ),
            "vpn_connectivity_win.json",
        )

        with patch("app.app.WorkflowPublicationService", return_value=publications):
            self.client.post("/api/workflow-favorites/vpn_connectivity_win")
            home = self.client.get("/").get_data(as_text=True)
            browse = self.client.get("/workflows").get_data(as_text=True)

        for html in (home, browse):
            self.assertIn("Internet Not Working", html)
            self.assertIn("VPN Not Connecting", html)
            self.assertNotIn("Advanced Network Diagnostics", html)
            self.assertNotIn("Higher-Layer Connectivity Diagnostics", html)
            self.assertNotIn('href="/wizard?workflow=vpn"', html)

    def test_current_public_metadata_is_consistent_across_catalog_search_and_wizard(self):
        expected = {
            "internet": ("Internet Not Working", "Windows"),
            "printer": ("Printer Not Working", "Windows"),
            "vpn_connectivity_win": ("VPN Not Connecting", "Windows"),
            "application_crash": ("Application Crashing or Freezing", "Windows"),
        }
        catalog = available_workflows()
        self.assertEqual(
            catalog["printer"]["description"],
            "Troubleshoot printer power, status, connections, printing, and "
            "stuck Windows print queues with verified recovery steps.",
        )
        self.assertEqual(
            catalog["vpn_connectivity_win"]["description"],
            "Troubleshoot Windows VPN connection, sign-in, client, adapter, "
            "security-software, and network problems, with verification after "
            "each approved step.",
        )

        for workflow_id, (title, platform) in expected.items():
            self.assertEqual(catalog[workflow_id]["workflow_id"], workflow_id)
            self.assertEqual(catalog[workflow_id]["name"], title)
            self.assertEqual(catalog[workflow_id]["platform"], platform)

            results = SearchService().search_all(title)
            self.assertTrue(any(
                item.id == workflow_id
                and item.content_type == "Workflow"
                and item.title == title
                for item in results
            ))

            client = app.test_client()
            response = client.get(f"/wizard?workflow={workflow_id}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(title, response.get_data(as_text=True))

        home = self.client.get("/").get_data(as_text=True)
        browse = self.client.get("/workflows").get_data(as_text=True)
        search = self.client.get(
            "/search?q=VPN+Not+Connecting&type=workflow"
        ).get_data(as_text=True)
        for html in (home, browse):
            self.assertNotIn("Application Keeps Crashing", html)
            self.assertNotIn("VPN Connectivity Troubleshooting (Windows)", html)
        self.assertIn("VPN Not Connecting", search)
        self.assertNotIn("VPN Connectivity Troubleshooting (Windows)", search)

    def test_current_publication_fingerprints_match_metadata_polish(self):
        publications = WorkflowPublicationService()
        for workflow_id in (
            "internet", "printer", "vpn_connectivity_win", "application_crash"
        ):
            snapshot = publications.load_current(workflow_id)
            self.assertEqual(
                snapshot["publication"]["content_hash"],
                publications.content_hash(snapshot["workflow"]),
            )

    def test_hidden_favorite_identity_is_preserved_but_not_recommended(self):
        with self.client.session_transaction() as browser_session:
            browser_session["favorite_workflow_ids"] = ["network_diagnostics"]

        html = self.client.get("/").get_data(as_text=True)

        self.assertNotIn("Advanced Network Diagnostics", html)
        with self.client.session_transaction() as browser_session:
            self.assertEqual(
                browser_session["favorite_workflow_ids"], ["network_diagnostics"]
            )

    def test_legacy_vpn_direct_route_and_pinned_version_remain_available(self):
        root = Path(self.temporary.name)
        publications = WorkflowPublicationService(root / "publications")
        version_two = self._workflow("vpn", "Legacy VPN Procedure")
        version_two["nodes"]["start"]["instruction"] = "Legacy version two guidance."
        publications.publish(self._workflow("vpn", "Initial VPN Procedure"), "vpn.json")
        publications.publish(version_two, "vpn.json")

        with patch("app.app.WorkflowPublicationService", return_value=publications):
            initial = self.client.get("/wizard?workflow=vpn")
            version_three = self._workflow("vpn", "New VPN Procedure")
            version_three["nodes"]["start"]["instruction"] = "New version three guidance."
            publications.publish(version_three, "vpn.json")
            resumed = self.client.get("/wizard?workflow=vpn&resume=1")

        self.assertEqual(initial.status_code, 200)
        self.assertIn("Legacy version two guidance.", initial.get_data(as_text=True))
        self.assertEqual(resumed.status_code, 200)
        self.assertIn("Legacy version two guidance.", resumed.get_data(as_text=True))
        self.assertNotIn("New version three guidance.", resumed.get_data(as_text=True))

    def test_search_returns_mature_vpn_workflow_not_legacy_vpn(self):
        root = Path(self.temporary.name)
        publications = WorkflowPublicationService(root / "publications")
        publications.publish(self._workflow("vpn", "Vpn"), "vpn.json")
        publications.publish(
            self._workflow(
                "vpn_connectivity_win", "VPN Connectivity Troubleshooting (Windows)"
            ),
            "vpn_connectivity_win.json",
        )
        search = SearchService()
        search.knowledge.get_published = lambda: []
        search.commands.get_all = lambda: []

        with patch(
            "app.services.search_service.WorkflowPublicationService",
            return_value=publications,
        ):
            workflow_results = [
                item for item in search.search_all("VPN")
                if item.content_type == "Workflow"
            ]

        self.assertEqual(
            [item.id for item in workflow_results], ["vpn_connectivity_win"]
        )

    def test_workflow_studio_keeps_legacy_and_public_vpn_drafts_visible(self):
        root = Path(self.temporary.name)
        drafts = WorkflowDraftService(root / "drafts")
        drafts.save_draft(self._workflow("vpn", "Vpn"))
        drafts.save_draft(self._workflow(
            "vpn_connectivity_win", "VPN Connectivity Troubleshooting (Windows)"
        ))

        with patch("app.app.WorkflowDraftService", return_value=drafts):
            response = self.client.get("/workflow-studio")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Vpn", html)
        self.assertIn("VPN Not Connecting", html)
        self.assertNotIn("VPN Connectivity Troubleshooting (Windows)", html)
        self.assertEqual(
            drafts.get_draft("vpn_connectivity_win.json")["name"],
            "VPN Connectivity Troubleshooting (Windows)",
        )

    def test_current_publications_override_fallback_and_discovery_renders_public_entries(self):
        publication_root = Path(self.temporary.name) / "publications"
        publications = WorkflowPublicationService(publication_root)
        identities = list(AVAILABLE_WORKFLOWS) + [f"published_{index}" for index in range(5)]
        for workflow_id in identities:
            name = "Current Internet Guidance" if workflow_id == "internet" else workflow_id.replace("_", " ").title()
            publications.publish(self._workflow(workflow_id, name), f"{workflow_id}.json")
        before = {
            path.relative_to(publication_root): path.read_bytes()
            for path in publication_root.rglob("*") if path.is_file()
        }

        with patch("app.app.WorkflowPublicationService", return_value=publications):
            catalog = available_workflows()
            response = self.client.get("/workflows")
            home = self.client.get("/")

        self.assertEqual(len(catalog), len(identities))
        self.assertEqual(catalog["internet"]["name"], "Internet Not Working")
        self.assertTrue(all(item["source"] == "published" for item in catalog.values()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True).count("workflow-card-item"), 12)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Internet Not Working", response.get_data(as_text=True))
        self.assertIn("Internet Not Working", home.get_data(as_text=True))
        self.assertIn("Explore all 12 workflows", home.get_data(as_text=True))
        after = {
            path.relative_to(publication_root): path.read_bytes()
            for path in publication_root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_missing_runtime_publications_use_fallback_without_creating_storage(self):
        publication_root = Path(self.temporary.name) / "not-created"
        publications = WorkflowPublicationService(publication_root)
        with patch("app.app.WorkflowPublicationService", return_value=publications):
            catalog = available_workflows()
        self.assertEqual(set(catalog), set(AVAILABLE_WORKFLOWS))
        self.assertTrue(all(item["source"] == "built_in" for item in catalog.values()))
        self.assertFalse(publication_root.exists())

    def test_malformed_current_publication_fails_closed_instead_of_showing_fallback(self):
        publication_root = Path(self.temporary.name) / "damaged-publications"
        workflow_root = publication_root / "internet"
        workflow_root.mkdir(parents=True)
        (workflow_root / "current.json").write_text('{"current_version": 1}', encoding="utf-8")
        (workflow_root / "v0001.json").write_text("{not-json", encoding="utf-8")
        publications = WorkflowPublicationService(publication_root)
        with patch("app.app.WorkflowPublicationService", return_value=publications):
            response = self.client.get("/workflows")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Saved data needs attention", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
