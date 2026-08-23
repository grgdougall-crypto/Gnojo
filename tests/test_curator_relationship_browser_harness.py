import hashlib
import html
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.services.curator_relationship_repair_browser_harness import phase3_browser_harness
from curator.memory import CuratorMemoryStore


class CuratorRelationshipBrowserHarnessTests(unittest.TestCase):
    URL = "/__dev/phase3-relationship-harness"

    def setUp(self):
        self.previous = {key: app.config.get(key) for key in ("TESTING", "DEBUG")}
        self.client = app.test_client()

    def tearDown(self):
        app.config.update(self.previous)

    @staticmethod
    def production_hashes():
        root = Path(__file__).parents[1]
        memory = root / "curation_memory" / "memory.json"
        paths = sorted([*(root / "knowledge_base" / "commands").glob("*.json"),
                        *(root / "knowledge_base" / "published").glob("*.json")])
        aggregate = hashlib.sha256(b"\n".join(path.read_bytes() for path in paths)).hexdigest()
        return hashlib.sha256(memory.read_bytes()).hexdigest(), aggregate

    def test_harness_is_404_without_both_explicit_flag_and_nonproduction_mode(self):
        for testing, debug, flag in ((False, False, "true"), (True, False, ""), (False, True, "")):
            with self.subTest(testing=testing, debug=debug, flag=flag), patch.dict(
                    os.environ, {"GNOJO_PHASE3_BROWSER_HARNESS": flag}, clear=False):
                app.config.update(TESTING=testing, DEBUG=debug)
                self.assertEqual(self.client.get(self.URL).status_code, 404)

    def test_real_queue_detail_apply_verify_history_and_reset_remain_temporary(self):
        before = self.production_hashes()
        app.config.update(TESTING=True, DEBUG=False)
        with patch.dict(os.environ, {"GNOJO_PHASE3_BROWSER_HARNESS": "true"}, clear=False):
            phase3_browser_harness().reset()
            queue = self.client.get(self.URL)
            self.assertEqual(queue.status_code, 200)
            queue_html = queue.get_data(as_text=True)
            for task_id, outcome in (("GKT-HARNESS-ADD", "Add Reciprocal"),
                                     ("GKT-HARNESS-REMOVE", "Remove Unsupported"),
                                     ("GKT-HARNESS-HUMAN", "Human Review Required")):
                self.assertIn(task_id, queue_html)
                self.assertIn(outcome, queue_html)
            self.assertIn("Temporary Phase 3 development/test fixtures", queue_html)

            detail_url = f"{self.URL}/tasks/GKT-HARNESS-ADD"
            detail_html = self.client.get(detail_url).get_data(as_text=True)
            self.assertIn("Add &#39;adapter-tool&#39; to related_commands.", detail_html)
            self.assertIn("knowledge_base/published/ethernet-link-check.json", detail_html)
            self.assertIn("Apply proposed relationship repair", detail_html)
            self.assertIn("I reviewed this exact metadata change", detail_html)
            token = html.unescape(re.search(r'name="approval_token" value="([^"]+)"', detail_html).group(1))

            response = self.client.post(f"{detail_url}/apply", data={
                "approval_token": token, "approved": "yes",
            }, follow_redirects=True)
            applied_html = response.get_data(as_text=True)
            self.assertIn("appears corrected", applied_html.casefold())
            self.assertIn("Relationship Repair Proposal Applied", applied_html)
            self.assertIn("Targeted Verification", applied_html)
            self.assertIn("Open", applied_html)
            root = phase3_browser_harness().root
            self.assertNotEqual(root, Path(__file__).parents[1].resolve())
            task = CuratorMemoryStore(root / "curation_memory").load()["tasks"]["GKT-HARNESS-ADD"]
            self.assertEqual(task["status"], "open")
            self.assertEqual(task["current_verification"]["status"], "appears_corrected")

            before_duplicate = (root / "knowledge_base/published/ethernet-link-check.json").read_bytes()
            duplicate = self.client.post(f"{detail_url}/apply", data={
                "approval_token": token, "approved": "yes",
            }, follow_redirects=True)
            self.assertIn("not applied", duplicate.get_data(as_text=True).casefold())
            self.assertEqual((root / "knowledge_base/published/ethernet-link-check.json").read_bytes(),
                             before_duplicate)

            human_html = self.client.get(f"{self.URL}/tasks/GKT-HARNESS-HUMAN").get_data(as_text=True)
            self.assertIn("Human Review Required", human_html)
            self.assertNotIn("Apply proposed relationship repair", human_html)

            old_root = root
            reset = self.client.post(f"{self.URL}/reset", follow_redirects=True)
            self.assertEqual(reset.status_code, 200)
            self.assertNotEqual(phase3_browser_harness().root, old_root)
            self.assertFalse(old_root.exists())
            reset_task = CuratorMemoryStore(
                phase3_browser_harness().root / "curation_memory"
            ).load()["tasks"]["GKT-HARNESS-ADD"]
            self.assertEqual(reset_task["status"], "open")
            self.assertNotIn("current_verification", reset_task)

        self.assertEqual(self.production_hashes(), before)


if __name__ == "__main__":
    unittest.main()
