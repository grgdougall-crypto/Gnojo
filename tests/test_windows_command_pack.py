import json
import html as html_module
import unittest
from pathlib import Path

from app.app import app
from app.repositories.command_repository import CommandRepository


class WindowsCommandPackTests(unittest.TestCase):
    EXPECTED_IDS = {
        "get-computerinfo", "get-volume", "get-process", "get-service",
        "get-winevent", "get-netadapter", "get-netipconfiguration",
        "test-netconnection", "resolve-dnsname", "get-printer",
        "get-printjob", "sfc-scannow", "dism-scanhealth", "chkdsk-scan",
        "ipconfig", "ping",
    }

    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.repository = CommandRepository()

    def test_pack_is_valid_and_contains_safety_metadata(self):
        commands = self.repository.get_all()
        by_id = {command["id"]: command for command in commands}
        self.assertTrue(self.EXPECTED_IDS.issubset(by_id))
        for command_id in self.EXPECTED_IDS:
            command = by_id[command_id]
            for field in (
                "name", "title", "summary", "category", "shell", "platforms",
                "syntax", "examples", "permissions", "risk", "output_fields",
                "common_errors", "related_commands", "tags", "sources",
            ):
                self.assertIn(field, command, f"{command_id}: {field}")
            self.assertIn(command["risk"]["level"], {"Low", "Moderate", "High"})
            self.assertIsInstance(command["risk"]["changes_system"], bool)
            self.assertTrue(command["sources"][0]["url"].startswith("https://"))

    def test_command_library_displays_pack_and_risk_badges(self):
        response = self.client.get("/commands")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Inspect Windows System Information", html)
        self.assertIn("Test a Network Host and Service Port", html)
        self.assertIn("Scan the Windows Component Store", html)
        self.assertIn("Risk:", html)

    def test_every_new_command_detail_page_loads(self):
        new_files = Path("knowledge_base/commands").glob("*.json")
        for path in new_files:
            command = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(command_id=command["id"]):
                response = self.client.get(f"/commands/{command['id']}")
                self.assertEqual(response.status_code, 200)
                page_html = response.get_data(as_text=True)
                self.assertIn(command["name"], page_html)
                self.assertIn(html_module.escape(command["syntax"]), page_html)

    def test_integrity_commands_show_elevation_and_change_warnings(self):
        sfc = self.repository.get("sfc-scannow")
        dism = self.repository.get("dism-scanhealth")
        self.assertTrue(sfc["permissions"]["requires_elevation"])
        self.assertTrue(sfc["risk"]["changes_system"])
        self.assertTrue(dism["permissions"]["requires_elevation"])
        self.assertFalse(dism["risk"]["changes_system"])

    def test_workflow_knowledge_panel_renders_structured_commands(self):
        html = self.client.get(
            "/wizard?workflow=network_diagnostics"
        ).get_data(as_text=True)

        self.assertIn("Inspect Windows Network Configuration with ipconfig", html)
        self.assertIn('<div class="knowledge-command">', html)
        self.assertIn("<code>ipconfig /all</code>", html)
        self.assertNotIn("{&#39;command&#39;:", html)


if __name__ == "__main__":
    unittest.main()
