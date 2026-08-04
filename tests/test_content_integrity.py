import json
import unittest
from collections import Counter
from pathlib import Path

from app.knowledge.article_validator import ArticleValidator
from app.services.workflow_validation_service import WorkflowValidationService


class ContentIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    @staticmethod
    def load_json_files(folder):
        return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(folder.glob("*.json"))]

    def test_all_content_json_is_parseable_and_ids_are_unique(self):
        groups = {
            "articles": self.load_json_files(self.root / "knowledge_base" / "published"),
            "commands": self.load_json_files(self.root / "knowledge_base" / "commands"),
            "workflows": self.load_json_files(self.root / "app" / "decision_trees"),
        }
        groups["scripts"] = [(self.root / "knowledge_base" / "scripts" / "catalog.json", item) for item in json.loads((self.root / "knowledge_base" / "scripts" / "catalog.json").read_text(encoding="utf-8"))]
        keys = {"articles": "id", "commands": "id", "workflows": "workflow_id", "scripts": "id"}
        for group, records in groups.items():
            values = [record.get(keys[group]) for _, record in records]
            self.assertNotIn(None, values, group)
            duplicates = [value for value, count in Counter(values).items() if count > 1]
            self.assertEqual(duplicates, [], group)

    def test_published_articles_follow_current_schema(self):
        for path, article in self.load_json_files(self.root / "knowledge_base" / "published"):
            with self.subTest(article=path.name):
                self.assertEqual(ArticleValidator.validate(article), [])

    def test_workflows_are_valid(self):
        validator = WorkflowValidationService()
        for path, workflow in self.load_json_files(self.root / "app" / "decision_trees"):
            with self.subTest(workflow=path.name):
                self.assertEqual(validator.validate(workflow)["errors"], [])

    def test_cross_content_relationships_resolve(self):
        articles = self.load_json_files(self.root / "knowledge_base" / "published")
        commands = self.load_json_files(self.root / "knowledge_base" / "commands")
        workflows = self.load_json_files(self.root / "app" / "decision_trees")
        scripts = json.loads((self.root / "knowledge_base" / "scripts" / "catalog.json").read_text(encoding="utf-8"))
        article_ids = {record["id"] for _, record in articles}
        command_ids = {record["id"] for _, record in commands}
        workflow_ids = {record["workflow_id"] for _, record in workflows}
        for path, command in commands:
            with self.subTest(command=path.name):
                self.assertTrue(set(command.get("related_commands", [])) <= command_ids)
                self.assertTrue(set(command.get("related_articles", [])) <= article_ids)
        for path, article in articles:
            with self.subTest(article=path.name):
                self.assertTrue(set(article.get("related_commands", [])) <= command_ids)
        for script in scripts:
            with self.subTest(script=script["id"]):
                self.assertTrue(set(script.get("related_commands", [])) <= command_ids)
                self.assertTrue(set(script.get("related_workflows", [])) <= workflow_ids)
                self.assertTrue((self.root / "knowledge_base" / "scripts" / script["filename"]).is_file())


if __name__ == "__main__":
    unittest.main()
