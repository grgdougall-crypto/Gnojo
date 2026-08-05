import unittest

from app.services.article_tag_service import ArticleTagService


class ArticleTagServiceTests(unittest.TestCase):
    def test_generates_specific_search_tags_from_article_content(self):
        tags = ArticleTagService.generate({
            "title": "How to Install the Approved Bluetooth Driver",
            "category": "Desktop Support",
            "overview": "Update a Windows Bluetooth adapter safely.",
            "checklist": [
                "Open Device Manager and update the driver.",
                "Check Windows Update.",
            ],
            "common_indicators": ["Bluetooth pairing fails."],
            "related_topics": [],
        })
        self.assertIn("bluetooth", tags)
        self.assertIn("device manager", tags)
        self.assertIn("windows update", tags)
        self.assertGreaterEqual(len(tags), 3)
        self.assertLessEqual(len(tags), 8)

    def test_normalizes_and_deduplicates_existing_tags(self):
        self.assertEqual(
            ArticleTagService.normalize(" Windows, bluetooth\nWINDOWS "),
            ["windows", "bluetooth"],
        )


if __name__ == "__main__":
    unittest.main()
