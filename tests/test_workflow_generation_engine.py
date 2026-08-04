import unittest

from flask import render_template

from app.app import app
from app.engine.workflow_generation_engine import WorkflowGenerationEngine


class StubProvider:
    def __init__(self, node_count=8):
        self.node_count = node_count

    def generate_workflow(self, **_kwargs):
        nodes = {
            f"done_{index}": {
                "type": "resolution",
                "title": "Complete",
                "message": "Complete.",
            }
            for index in range(self.node_count)
        }
        return {
            "workflow_id": "bluetooth_test",
            "name": "bluetooth device not connecting",
            "estimated_steps": 5,
            "start_node": "done_0",
            "nodes": nodes,
        }


class WorkflowGenerationEngineTests(unittest.TestCase):
    def test_generation_normalizes_title_and_preserves_requested_metadata(self):
        engine = WorkflowGenerationEngine.__new__(WorkflowGenerationEngine)
        engine.primary_provider = StubProvider(14)
        engine.fallback_provider = StubProvider(14)

        workflow = engine.generate_workflow(
            "bluetooth device not connecting",
            description="Troubleshoot Bluetooth pairing.",
            platform="Windows",
            difficulty="Intermediate",
            size="Medium",
        )

        self.assertEqual(workflow["name"], "Bluetooth Device Not Connecting")
        self.assertEqual(workflow["description"], "Troubleshoot Bluetooth pairing.")
        self.assertEqual(workflow["platform"], "Windows")
        self.assertEqual(workflow["difficulty"], "Intermediate")
        self.assertEqual(workflow["size"], "Medium")

    def test_each_workflow_size_accepts_only_its_configured_range(self):
        for size, accepted_count in (("Small", 8), ("Medium", 14), ("Large", 22)):
            with self.subTest(size=size):
                workflow = {"nodes": {str(index): {} for index in range(accepted_count)}}
                WorkflowGenerationEngine._enforce_size(workflow, size)

        for size, rejected_count in (("Small", 13), ("Medium", 21), ("Large", 31)):
            with self.subTest(size=size):
                workflow = {"nodes": {str(index): {} for index in range(rejected_count)}}
                with self.assertRaisesRegex(ValueError, "required range"):
                    WorkflowGenerationEngine._enforce_size(workflow, size)

    def test_oversized_primary_response_uses_valid_fallback(self):
        engine = WorkflowGenerationEngine.__new__(WorkflowGenerationEngine)
        engine.primary_provider = StubProvider(34)
        engine.fallback_provider = StubProvider(20)

        workflow = engine.generate_workflow(
            "Bluetooth device not connecting",
            size="Medium",
        )

        self.assertEqual(len(workflow["nodes"]), 20)
        self.assertEqual(workflow["generation_provider"], "OpenAI")

    def test_title_case_preserves_common_technical_terms(self):
        self.assertEqual(
            WorkflowGenerationEngine._title_case("vpn and dns troubleshooting for macos"),
            "VPN and DNS Troubleshooting for macOS",
        )

    def test_valid_generation_links_directly_to_human_review(self):
        with app.test_request_context(
            "/workflow-builder",
            method="POST",
            data={
                "workflow_name": "webcam not working",
                "description": "Troubleshoot a webcam.",
                "platform": "Windows",
                "difficulty": "Beginner",
                "size": "Small",
            },
        ):
            html = render_template(
                "workflow_builder.html",
                generated_workflow={"name": "Webcam Not Working"},
                validation={"is_valid": True, "warnings": []},
                outline=[],
                filename="webcam_not_working_windows.json",
                error=None,
            )

        self.assertIn("Review and Prepare to Publish", html)
        self.assertIn(
            'href="/workflow-editor/webcam_not_working_windows.json"',
            html,
        )
        self.assertIn("Return to Workflow Studio", html)


if __name__ == "__main__":
    unittest.main()
