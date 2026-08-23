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
             "Return to Maintenance"),
            ("assisted_resolution", "/curator/tasks/GKT-TEST#assisted-resolution",
             "Return to Assisted Resolution"),
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


if __name__ == "__main__":
    unittest.main()
