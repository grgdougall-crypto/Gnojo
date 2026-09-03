import html
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from app.app import app
from curator.memory import CuratorMemoryStore


class CrossWorkspaceNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous_root = app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        app.config.update(TESTING=True, STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root))

        source_root = Path(__file__).resolve().parents[1]
        for relative in (
            "knowledge_base/commands/ping.json",
            "knowledge_base/published/ethernet-connection-check.json",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, target)
        self.trusted_content = {
            path: path.read_bytes()
            for path in (self.root / "knowledge_base").rglob("*.json")
        }

        shared = ["ping", "ethernet-connection-check"]
        tasks = {
            "GKT-SOURCE": self._task("GKT-SOURCE", "Source relationship review", shared),
            "GKT-RELATED": self._task("GKT-RELATED", "Related relationship review", shared),
        }
        store = CuratorMemoryStore(self.root / "curation_memory")
        state = store.load()
        state["tasks"] = tasks
        store.save(state)
        self.memory_path = self.root / "curation_memory" / "memory.json"
        self.client = app.test_client()
        self.relationship_patch = patch(
            "app.services.curator_targeted_verification_service."
            "CuratorTargetedVerificationService.relationship_evidence",
            return_value={
                "heading": "Current relationship declarations",
                "target_found": True,
                "affected_type": "command",
                "affected_id": "ping",
                "related_articles": ["ethernet-connection-check"],
                "related_commands": [],
                "commands": [],
                "command_context": {
                    "id": "ping", "name": "ping", "title": "Ping",
                    "summary": "Tests IP reachability.", "category": "Networking",
                    "platforms": ["Windows"], "tags": ["network"],
                },
                "articles": [{
                    "id": "ethernet-connection-check", "found": True,
                    "title": "Ethernet Connection Check",
                    "overview": "Check a wired Ethernet link.", "category": "Networking",
                    "tags": ["ethernet"], "structured_commands": ["ipconfig"],
                    "related_commands": [],
                }],
            },
        )
        self.relationship_patch.start()

    def tearDown(self):
        self.relationship_patch.stop()
        if self.previous_root is None:
            app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = self.previous_root
        self.temporary.cleanup()

    @staticmethod
    def _task(task_id, title, related):
        return {
            "task_id": task_id, "finding_id": f"CUR-{task_id}", "status": "open",
            "owner": "Human", "priority": "Medium", "classification": "Risk",
            "finding_type": "article_command_reciprocity_conflict",
            "title": title, "content_type": "command", "content_identifier": "ping",
            "curator_rule": "CUR-REL-ARTICLE-COMMAND-RECIPROCITY",
            "explanation": "Review the explicit relationship.",
            "recommended_action": "Review current declarations.", "confidence": "high",
            "knowledge_debt_score": 5, "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00", "times_observed": 1,
            "related_content": related, "related_workflows": [],
            "related_articles": ["ethernet-connection-check"],
            "related_commands": ["ping"], "related_scripts": [],
            "evidence": ["Relationship declarations differ."],
            "history": [{"event": "observed", "at": "2026-01-01T00:00:00+00:00"}],
            "resolution_history": [],
        }

    @staticmethod
    def _href_for(text, markup):
        class AnchorParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.current_href = None
                self.current_text = []
                self.anchors = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.current_href = dict(attrs).get("href", "")
                    self.current_text = []

            def handle_data(self, data):
                if self.current_href is not None:
                    self.current_text.append(data)

            def handle_endtag(self, tag):
                if tag == "a" and self.current_href is not None:
                    self.anchors.append((self.current_href, " ".join(self.current_text)))
                    self.current_href = None

        parser = AnchorParser()
        parser.feed(markup)
        match = next((href for href, label in parser.anchors if text in label), None)
        if match is None:
            raise AssertionError(f"Could not find link containing {text!r}")
        return html.unescape(match)

    def test_supporting_article_and_command_round_trip_preserve_task_and_workspace(self):
        before = self.memory_path.read_bytes()
        source_url = (
            "/curator/tasks/GKT-SOURCE?origin=content_quality"
            "&return_to=/content-quality%23queueTitle"
        )
        source_html = self.client.get(source_url).get_data(as_text=True)
        self.assertIn("Show related items", source_html)
        self.assertIn("Hide related items", source_html)
        self.assertIn("curator-related-disclosure__toggle", source_html)

        article_url = self._href_for("View article:", source_html)
        article_html = self.client.get(article_url).get_data(as_text=True)
        self.assertIn("Return to Curator task", article_html)
        task_url = self._href_for("Return to Curator task", article_html)
        self.assertIn("/curator/tasks/GKT-SOURCE", task_url)
        self.assertIn("origin=content_quality", task_url)
        manage_url = self._href_for("Manage in Integrity", article_html)
        manage_html = self.client.get(manage_url).get_data(as_text=True)
        self.assertIn("You are managing this published article in Knowledge Integrity", manage_html)
        article_return = self._href_for("Return to article", manage_html)
        returned_article = self.client.get(article_return).get_data(as_text=True)
        self.assertIn("Return to Curator task", returned_article)
        returned_task = self.client.get(task_url).get_data(as_text=True)
        self.assertIn("Return to Content Quality", returned_task)
        self.assertIn('href="/content-quality#queueTitle"', returned_task)

        command_url = self._href_for("View command:", source_html)
        command_html = self.client.get(command_url).get_data(as_text=True)
        self.assertIn("Return to Curator task", command_html)
        command_task_url = self._href_for("Return to Curator task", command_html)
        self.assertIn("/curator/tasks/GKT-SOURCE", command_task_url)
        self.assertIn("origin=content_quality", command_task_url)
        self.assertEqual(self.memory_path.read_bytes(), before)
        self.assertEqual(
            {path: path.read_bytes() for path in self.trusted_content},
            self.trusted_content,
        )

    def test_related_task_round_trip_is_bounded_and_preserves_original_workspace(self):
        before = self.memory_path.read_bytes()
        source_html = self.client.get(
            "/curator/tasks/GKT-SOURCE?origin=content_quality"
            "&return_to=/content-quality%23queueTitle"
        ).get_data(as_text=True)
        related_url = self._href_for("Related relationship review", source_html)
        self.assertIn("origin=previous_task", related_url)

        related_html = self.client.get(related_url).get_data(as_text=True)
        self.assertIn("Return to previous task", related_html)
        previous_url = self._href_for("Return to previous task", related_html)
        self.assertIn("/curator/tasks/GKT-SOURCE", previous_url)
        self.assertIn("origin=content_quality", previous_url)
        previous_html = self.client.get(previous_url).get_data(as_text=True)
        self.assertIn("Return to Content Quality", previous_html)
        self.assertEqual(self.memory_path.read_bytes(), before)
        self.assertEqual(
            {path: path.read_bytes() for path in self.trusted_content},
            self.trusted_content,
        )


if __name__ == "__main__":
    unittest.main()
