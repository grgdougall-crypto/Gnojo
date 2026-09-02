import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.repositories.script_repository import ScriptRepository
from app.services.script_authoring_service import ScriptAuthoringService


class ScriptBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temporary.name)
        (self.base_path / "catalog.json").write_text("[]", encoding="utf-8")
        self.repository = ScriptRepository(self.base_path)
        self.service = ScriptAuthoringService(self.base_path)
        self.repository_patch = patch("app.app.script_repository", self.repository)
        self.service_patch = patch("app.app.script_authoring_service", self.service)
        self.repository_patch.start()
        self.service_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.service_patch.stop()
        self.repository_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def valid_form(action="validate"):
        return {
            "action": action, "name": "Test Service Restart", "script_id": "test-service-restart",
            "kind": "Automation", "category": "Administration",
            "platform": "Windows", "language": "PowerShell",
            "summary": "Restarts one test service after confirmation.",
            "source": "[CmdletBinding(SupportsShouldProcess=$true)]\nparam([string]$ServiceName)\nif ($PSCmdlet.ShouldProcess($ServiceName, 'Restart')) { Write-Host 'Test' }\n",
            "collects": "Validates the service\nRestarts after confirmation",
            "parameters": "ServiceName | required | Exact service name",
            "changes": "Restarts the selected service",
            "dry_run": "Run with -WhatIf first.", "rollback": "Record errors and follow the service recovery plan.",
            "requires_elevation": "on", "permission_notes": "Run as administrator.",
            "privacy_note": "Review organization-specific service names before sharing.",
            "related_commands": "get-service", "related_workflows": "",
        }

    def test_builder_is_linked_from_studio_and_library(self):
        studio = self.client.get("/content-studio").get_data(as_text=True)
        library = self.client.get("/scripts").get_data(as_text=True)
        builder = self.client.get("/scripts/builder")
        self.assertIn('href="/scripts/builder"', studio)
        self.assertIn("Open Workflow Studio", studio)
        self.assertIn("Open Article Builder Preview", studio)
        self.assertIn("Article Builder Preview", self.client.get("/knowledge/builder").get_data(as_text=True))
        self.assertIn("Open Command Builder", studio)
        self.assertIn("Open Script Builder", studio)
        self.assertIn("Build and review automation and diagnostic scripts.", studio)
        self.assertNotIn("Use reviewed automation and diagnostic scripts.", studio)
        self.assertIn('href="/scripts/builder"', library)
        self.assertEqual(builder.status_code, 200)
        self.assertIn("Validate and preview", builder.get_data(as_text=True))

    def test_valid_automation_can_be_previewed_and_published(self):
        preview = self.client.post("/scripts/builder", data=self.valid_form())
        html = preview.get_data(as_text=True)
        self.assertIn("Validation passed", html)
        self.assertIn("Publish to library", html)
        published = self.client.post("/scripts/builder", data=self.valid_form("publish"), follow_redirects=False)
        self.assertEqual(published.status_code, 302)
        self.assertIn("/scripts/test-service-restart", published.headers["Location"])
        record = self.repository.get("test-service-restart")
        self.assertEqual(record["kind"], "Automation")
        self.assertIn("SupportsShouldProcess", record["source"])
        catalog = json.loads((self.base_path / "catalog.json").read_text(encoding="utf-8"))
        self.assertNotIn("source", catalog[0])

    def test_unsafe_automation_is_blocked(self):
        form = self.valid_form("publish")
        form["source"] = "param([string]$ServiceName)\nRestart-Service $ServiceName\n"
        form["dry_run"] = ""
        form["rollback"] = ""
        response = self.client.post("/scripts/builder", data=form)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("must document preview or dry-run behavior", html)
        self.assertIn("must use SupportsShouldProcess", html)
        self.assertEqual(self.repository.get_all(), [])

    def test_bad_syntax_duplicate_ids_and_unknown_relationships_are_blocked(self):
        form = self.valid_form()
        form["source"] = "param(\n"
        form["related_commands"] = "not-a-command"
        response = self.client.post("/scripts/builder", data=form)
        html = response.get_data(as_text=True)
        self.assertIn("PowerShell syntax", html)
        self.assertIn("Unknown related command IDs", html)

    def test_linux_bash_automation_publishes_as_shell_script(self):
        form = self.valid_form("publish")
        form.update({
            "name": "Linux Cache Preview", "script_id": "linux-cache-preview",
            "platform": "Linux", "language": "Bash", "related_commands": "",
            "source": "#!/usr/bin/env bash\nset -euo pipefail\nDRY_RUN=false\n[[ ${1:-} == --dry-run ]] && DRY_RUN=true\nprintf '%s\\n' \"dry run: $DRY_RUN\"\n",
            "dry_run": "Use --dry-run to preview.",
        })
        response = self.client.post("/scripts/builder", data=form, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        record = self.repository.get("linux-cache-preview")
        self.assertEqual(record["platform"], "Linux")
        self.assertEqual(record["language"], "Bash")
        self.assertEqual(record["filename"], "linux-cache-preview.sh")

    def test_invalid_platform_language_combination_is_blocked(self):
        form = self.valid_form()
        form["platform"] = "Windows"
        form["language"] = "Zsh"
        form["source"] = "#!/usr/bin/env zsh\nset -euo pipefail\n"
        response = self.client.post("/scripts/builder", data=form)
        self.assertIn("Zsh is not supported for Windows scripts", response.get_data(as_text=True))
        self.assertEqual(self.repository.get_all(), [])


if __name__ == "__main__":
    unittest.main()
