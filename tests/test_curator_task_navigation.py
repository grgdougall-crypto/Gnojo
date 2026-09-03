import unittest

from app.services.curator_task_navigation_service import CuratorTaskNavigationService


class CuratorTaskNavigationServiceTests(unittest.TestCase):
    TASK_ID = "GKT-TEST"

    def resolve(self, origin, return_to):
        return CuratorTaskNavigationService.resolve(origin, return_to, task_id=self.TASK_ID)

    def test_supported_origins_preserve_their_safe_working_context(self):
        cases = (
            ("knowledge_tasks", "/curator?status=open&q=dns&sort=debt#knowledge-tasks",
             "Return to Knowledge Tasks"),
            ("relationship_proposals",
             "/curator/relationship-proposals?outcome=add_reciprocal&status=open",
             "Return to Relationship Proposals"),
            ("maintenance", "/curator/fix/CFX-123?category=safety_risk&item=FIX-1",
             "Return to Fix Wizard"),
            ("assisted_resolution", "/curator/tasks/GKT-TEST#assisted-resolution",
             "Return to Assisted Resolution"),
            ("assisted_resolution_batch", "/curator#assisted-resolution-batch",
             "Return to Assisted Resolution Batch"),
            ("content_quality", "/content-quality#queueTitle",
             "Return to Content Quality"),
        )
        for origin, return_to, label in cases:
            with self.subTest(origin=origin):
                navigation = self.resolve(origin, return_to)
                self.assertEqual(navigation.origin, origin)
                self.assertEqual(navigation.return_url, return_to)
                self.assertEqual(navigation.return_label, label)

    def test_direct_task_and_invalid_destinations_fall_back_to_overview(self):
        for origin, return_to in (
            ("", ""),
            ("relationship_proposals", "https://evil.example/steal"),
            ("maintenance", "//evil.example/steal"),
            ("knowledge_tasks", "/curator?unsupported=1#knowledge-tasks"),
            ("maintenance", "/curator/fix/CFX-1/../../admin"),
            ("assisted_resolution", "/curator/tasks/GKT-OTHER#assisted-resolution"),
            ("assisted_resolution_batch", "/curator?status=open#assisted-resolution-batch"),
            ("assisted_resolution_batch", "/curator#knowledge-tasks"),
            ("content_quality", "https://evil.example/content-quality"),
            ("content_quality", "/content-quality?unsupported=1#queueTitle"),
            ("previous_task", "https://evil.example/curator/tasks/GKT-OTHER"),
            ("previous_task", "/curator/tasks/GKT-TEST"),
        ):
            with self.subTest(origin=origin, return_to=return_to):
                navigation = self.resolve(origin, return_to)
                self.assertEqual(navigation.origin, "overview")
                self.assertEqual(navigation.return_url, "/curator")
                self.assertEqual(navigation.return_label, "Return to Curator Overview")

    def test_assisted_resolution_round_trip_keeps_original_task_origin(self):
        navigation = self.resolve(
            "relationship_proposals",
            "/curator/relationship-proposals?outcome=remove_unsupported&status=deferred",
        )
        return_to = CuratorTaskNavigationService.assisted_task_return(self.TASK_ID, navigation)
        self.assertEqual(CuratorTaskNavigationService.valid_assisted_return(return_to), return_to)
        self.assertIn("origin=relationship_proposals", return_to)
        self.assertTrue(return_to.endswith("#assisted-resolution"))

    def test_maintenance_return_validation_rejects_malformed_paths(self):
        valid = "/curator/fix/CFX-123?category=safety_risk&item=FIX-1"
        self.assertEqual(CuratorTaskNavigationService.valid_maintenance_return(valid), valid)
        for value in ("https://evil.example/", "/curator/fix/CFX-1/../../admin",
                      "/curator/fix/CFX-1?unexpected=1"):
            self.assertEqual(CuratorTaskNavigationService.valid_maintenance_return(value), "")

    def test_task_return_preserves_validated_workspace_context(self):
        navigation = self.resolve("content_quality", "/content-quality#queueTitle")
        task_return = CuratorTaskNavigationService.task_return(self.TASK_ID, navigation)
        self.assertEqual(CuratorTaskNavigationService.valid_task_return(task_return), task_return)
        self.assertIn("origin=content_quality", task_return)
        self.assertIn("return_to=%2Fcontent-quality%23queueTitle", task_return)

    def test_previous_task_return_is_validated_and_bounded_to_one_hop(self):
        queue_navigation = self.resolve(
            "relationship_proposals",
            "/curator/relationship-proposals?outcome=add_reciprocal&status=open",
        )
        previous_return = CuratorTaskNavigationService.previous_task_return(
            self.TASK_ID, queue_navigation,
        )
        related_navigation = CuratorTaskNavigationService.resolve(
            "previous_task", previous_return, task_id="GKT-RELATED",
        )
        self.assertEqual(related_navigation.origin, "previous_task")
        self.assertEqual(related_navigation.return_label, "Return to previous task")
        self.assertIn("origin=relationship_proposals", related_navigation.return_url)

        nested_return = CuratorTaskNavigationService.task_return(
            "GKT-RELATED", related_navigation,
        )
        self.assertEqual(
            CuratorTaskNavigationService.resolve(
                "previous_task", nested_return, task_id="GKT-THIRD",
            ).origin,
            "overview",
        )

    def test_general_task_return_rejects_external_and_unsupported_nested_context(self):
        for value in (
            "https://evil.example/curator/tasks/GKT-TEST",
            "/curator/tasks/GKT-TEST?origin=content_quality&return_to=https%3A%2F%2Fevil.example",
            "/curator/tasks/GKT-TEST?origin=unsupported&return_to=%2Fcurator",
            "/curator/tasks/GKT-TEST?unexpected=1",
            "/curator/tasks/%2E%2E?origin=content_quality&return_to=%2Fcontent-quality",
        ):
            with self.subTest(value=value):
                self.assertEqual(CuratorTaskNavigationService.valid_task_return(value), "")

    def test_published_context_is_allowlisted(self):
        valid = "/knowledge/published?q=dns&category=Networking"
        detail = (
            "/knowledge/published/dns-basics?return_to="
            "%2Fknowledge%2Fpublished%3Fq%3Ddns%26category%3DNetworking"
        )
        self.assertEqual(CuratorTaskNavigationService.valid_published_context(valid), valid)
        self.assertEqual(CuratorTaskNavigationService.valid_published_context(detail), detail)
        for value in (
            "https://evil.example/knowledge/published",
            "/knowledge/published?redirect=https%3A%2F%2Fevil.example",
            "/knowledge/published/dns-basics?return_to=https%3A%2F%2Fevil.example",
        ):
            self.assertEqual(CuratorTaskNavigationService.valid_published_context(value), "")


if __name__ == "__main__":
    unittest.main()
