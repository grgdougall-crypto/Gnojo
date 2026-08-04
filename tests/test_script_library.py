import unittest
from pathlib import Path

from app.app import app
from app.repositories.command_repository import CommandRepository
from app.repositories.script_repository import ScriptRepository


class ScriptLibraryTests(unittest.TestCase):
    COLLECTORS = {
        "windows-system-snapshot", "performance-diagnostic-report",
        "network-connectivity-report", "disk-space-assessment",
        "application-crash-evidence", "printer-diagnostic-report",
    }
    AUTOMATIONS = {"clear-printer-queue", "restart-windows-service", "map-network-drive"}

    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.repository = ScriptRepository()

    def test_catalog_contains_six_low_risk_collectors(self):
        scripts = self.repository.get_all()
        self.assertEqual({item["id"] for item in scripts}, self.COLLECTORS | self.AUTOMATIONS)
        commands = CommandRepository()
        for script in scripts:
            if script.get("kind") == "Automation":
                self.assertEqual(script["risk"]["level"], "Moderate")
                self.assertTrue(script["risk"]["changes_system"])
                self.assertTrue(script["parameters"])
                self.assertTrue(script["dry_run"])
                self.assertTrue(script["rollback"])
            else:
                self.assertEqual(script["risk"]["level"], "Low")
                self.assertFalse(script["risk"]["changes_system"])
            self.assertTrue(script["privacy_note"])
            self.assertTrue(script["collects"])
            for command_id in script["related_commands"]:
                self.assertIsNotNone(commands.get(command_id), f"{script['id']}: {command_id}")

    def test_content_studio_opens_real_script_library(self):
        response = self.client.get("/content-studio")
        html = response.get_data(as_text=True)
        self.assertIn('href="/scripts/builder"', html)
        self.assertIn("Open Script Builder", html)
        self.assertNotIn("Coming Soon", html)
        library = self.client.get("/scripts")
        self.assertEqual(library.status_code, 200)
        self.assertIn("Automation Script Library", library.get_data(as_text=True))
        self.assertIn("Diagnostic Collectors", library.get_data(as_text=True))
        self.assertIn("Clear a Stuck Print Queue", library.get_data(as_text=True))
        self.assertIn("Network Connectivity Report", library.get_data(as_text=True))

    def test_every_script_has_preview_and_safe_download(self):
        for record in self.repository.get_all():
            with self.subTest(script_id=record["id"]):
                detail = self.client.get(f"/scripts/{record['id']}")
                self.assertEqual(detail.status_code, 200)
                html = detail.get_data(as_text=True)
                self.assertIn(record["name"], html)
                self.assertIn("Complete source", html)
                self.assertIn("script-source-view", html)
                self.assertIn("Privacy note", html)
                download = self.client.get(f"/scripts/{record['id']}/download")
                self.assertEqual(download.status_code, 200)
                self.assertIn("attachment", download.headers["Content-Disposition"])
                if record.get("kind") == "Automation":
                    self.assertIn(b"SupportsShouldProcess=$true", download.data)
                else:
                    self.assertIn(b"Purpose: Read-only", download.data)
                download.close()

    def test_repository_rejects_unsafe_source_paths(self):
        with self.assertRaises(ValueError):
            self.repository.source_path({"filename": "../outside.ps1"})
        with self.assertRaises(ValueError):
            self.repository.source_path({"filename": "catalog.json"})
        self.assertEqual(self.repository.source_path({"filename": "example.sh"}).suffix, ".sh")
        self.assertEqual(self.client.get("/scripts/not-real").status_code, 404)

    def test_sources_do_not_contain_remediation_commands(self):
        blocked = (
            "Remove-Item", "Stop-Process", "Stop-Service", "Restart-Service",
            "Set-Net", "Disable-Net", "Enable-Net", "Clear-PrintJob",
            "Remove-PrintJob", "Repair-Volume", "Format-Volume",
        )
        for record in self.repository.get_all():
            if record.get("kind") == "Automation":
                continue
            source = Path("knowledge_base/scripts", record["filename"]).read_text(encoding="utf-8")
            for command in blocked:
                self.assertNotIn(command, source, f"{record['id']}: {command}")

    def test_automations_require_preview_confirmation_and_recovery(self):
        for record in self.repository.get_all():
            if record.get("kind") != "Automation":
                continue
            source = Path("knowledge_base/scripts", record["filename"]).read_text(encoding="utf-8")
            self.assertIn("SupportsShouldProcess=$true", source)
            self.assertIn("ShouldProcess", source)
            self.assertTrue(record["risk"]["changes_system"])
            page = self.client.get(f"/scripts/{record['id']}").get_data(as_text=True)
            self.assertIn("What it changes", page)
            self.assertIn("Required inputs", page)
            self.assertIn("Rollback and recovery", page)
            self.assertIn("-WhatIf", page)


if __name__ == "__main__":
    unittest.main()
