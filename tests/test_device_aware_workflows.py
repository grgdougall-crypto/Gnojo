import tempfile
import unittest
from unittest.mock import patch

from app.app import app, workflow_device_compatibility
from app.services.device_profile_service import DeviceProfileService


class DeviceAwareWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.devices = DeviceProfileService(self.temp.name)
        self.device = self.devices.create({
            "name": "Windows laptop", "device_type": "Laptop", "platform": "Windows", "os_version": "11",
            "connection_type": "Wi-Fi", "manufacturer": "", "model": "", "notes": "",
        })
        self.client = app.test_client()
        self.device_patch = patch("app.app.DeviceProfileService", return_value=self.devices)
        self.device_patch.start()
        with self.client.session_transaction() as session:
            session["active_device_profile_id"] = self.device["id"]

    def tearDown(self):
        self.device_patch.stop()
        self.temp.cleanup()

    def test_compatibility_classification(self):
        self.assertEqual(workflow_device_compatibility({"platform": "Windows"}, self.device), "recommended")
        self.assertEqual(workflow_device_compatibility({"platform": "Cross-platform"}, self.device), "compatible")
        self.assertEqual(workflow_device_compatibility({"platform": "macOS"}, self.device), "incompatible")
        self.assertEqual(workflow_device_compatibility({"platform": "macOS"}, None), "neutral")

    def test_home_prioritizes_and_labels_matching_workflow(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Recommended for Windows laptop", html)
        recommended_position = html.find('data-compatibility="recommended"')
        incompatible_position = html.find('data-compatibility="incompatible"')
        self.assertGreaterEqual(recommended_position, 0)
        if incompatible_position >= 0:
            self.assertLess(recommended_position, incompatible_position)

    def test_mismatch_warns_and_user_can_override(self):
        catalog = {
            "internet": {
                "name": "Internet Connection", "description": "Test", "icon": "bi-wifi",
                "category": "Networking", "platform": "macOS", "source": "built_in",
            }
        }
        with patch("app.app.available_workflows", return_value=catalog):
            warning = self.client.get("/wizard?workflow=internet")
            self.assertEqual(warning.status_code, 200)
            self.assertIn("targets a different platform", warning.get_data(as_text=True))
            continued = self.client.get("/wizard?workflow=internet&override=1")
            self.assertEqual(continued.status_code, 200)
            self.assertIn("Internet Connection", continued.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
