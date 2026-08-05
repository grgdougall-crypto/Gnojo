import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app
from app.engine.decision_engine import DecisionEngine
from app.services.device_profile_service import DeviceProfileService
from app.services.workflow_condition_service import WorkflowConditionError, resolve_applicable_node
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import WorkflowPublicationService
from app.services.workflow_validation_service import WorkflowValidationService


def conditional_workflow():
    return {
        "workflow_id": "conditional_test", "name": "Conditional Test", "category": "Desktop Support",
        "platform": "Cross-platform", "estimated_steps": 2, "start_node": "mac_step",
        "nodes": {
            "mac_step": {"type": "instruction", "title": "macOS only step", "instruction": "Open Terminal.", "next": "done", "conditions": {"platform": "macOS"}, "skip_to": "windows_step"},
            "windows_step": {"type": "instruction", "title": "Windows step", "instruction": "Open Settings.", "next": "done"},
            "done": {"type": "resolution", "title": "Complete", "message": "Finished."},
        },
    }


class ProfileAwareRoutingTests(unittest.TestCase):
    def test_conditional_skip_destination_is_reachable(self):
        result = WorkflowValidationService().validate(
            conditional_workflow()
        )

        self.assertTrue(result["is_valid"])
        self.assertEqual(result["unreachable_nodes"], [])
        self.assertIn("windows_step", result["reachable_nodes"])

    def test_resolution_skips_mismatched_nodes_but_not_without_profile(self):
        workflow = conditional_workflow()
        engine = DecisionEngine(); engine.load_workflow_data(workflow)
        node, skipped = resolve_applicable_node(engine, "mac_step", {"platform": "Windows", "device_type": "Laptop", "connection_type": "Wi-Fi"})
        self.assertEqual(node.id, "windows_step")
        self.assertEqual(skipped[0]["id"], "mac_step")
        generic, generic_skips = resolve_applicable_node(engine, "mac_step", None)
        self.assertEqual(generic.id, "mac_step")
        self.assertEqual(generic_skips, [])

    def test_validation_rejects_missing_fallback_and_conditional_loops(self):
        workflow = conditional_workflow()
        del workflow["nodes"]["mac_step"]["skip_to"]
        result = WorkflowValidationService().validate(workflow)
        self.assertTrue(any("requires skip_to" in error for error in result["errors"]))

        workflow = conditional_workflow()
        workflow["nodes"]["windows_step"].update({"conditions": {"platform": "Windows"}, "skip_to": "mac_step"})
        result = WorkflowValidationService().validate(workflow)
        self.assertTrue(any("contains a loop" in error for error in result["errors"]))

    def test_conditions_persist_through_node_editing(self):
        with tempfile.TemporaryDirectory() as drafts:
            Path(drafts, "conditional.json").write_text(json.dumps(conditional_workflow()), encoding="utf-8")
            service = WorkflowDraftService(drafts)
            workflow = service.update_node("conditional.json", "windows_step", {
                "title": "Windows step", "instruction": "Open Settings.", "help_text": "", "next": "done",
                "knowledge_article": "", "next_workflow": "", "message": "", "question": "",
                "conditions": {"platform": "Windows", "device_type": "Laptop"}, "skip_to": "done",
            })
            self.assertEqual(workflow["nodes"]["windows_step"]["conditions"]["device_type"], "Laptop")
            self.assertEqual(workflow["nodes"]["windows_step"]["skip_to"], "done")

    def test_published_runtime_skips_and_explains(self):
        with tempfile.TemporaryDirectory() as publications_dir, tempfile.TemporaryDirectory() as devices_dir:
            publications = WorkflowPublicationService(publications_dir)
            publications.publish(conditional_workflow(), "conditional.json")
            devices = DeviceProfileService(devices_dir)
            device = devices.create({"name": "Windows laptop", "device_type": "Laptop", "platform": "Windows", "os_version": "11", "connection_type": "Wi-Fi", "manufacturer": "", "model": "", "notes": ""})
            client = app.test_client()
            with client.session_transaction() as session:
                session["active_device_profile_id"] = device["id"]
            with patch("app.app.WorkflowPublicationService", return_value=publications), patch("app.app.DeviceProfileService", return_value=devices):
                response = client.get("/wizard?workflow=conditional_test")
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Windows step", html)
            self.assertNotIn("Open Terminal.", html)
            self.assertIn("Gnojo adapted this workflow", html)
            self.assertIn("macOS only step", html)

    def test_runtime_detects_conditional_loop(self):
        workflow = conditional_workflow()
        workflow["nodes"]["windows_step"].update({"conditions": {"platform": "macOS"}, "skip_to": "mac_step"})
        engine = DecisionEngine(); engine.load_workflow_data(workflow)
        with self.assertRaises(WorkflowConditionError):
            resolve_applicable_node(engine, "mac_step", {"platform": "Windows"})


if __name__ == "__main__":
    unittest.main()
