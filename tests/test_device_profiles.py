import tempfile
import unittest
from unittest.mock import patch

from app.app import app
from app.services.device_profile_service import DeviceProfileError, DeviceProfileService


class DeviceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = DeviceProfileService(self.temp.name)
        self.client = app.test_client()
        self.patch = patch("app.app.DeviceProfileService", return_value=self.service)
        self.patch.start()
        self.payload = {
            "name": "Work laptop", "device_type": "Laptop", "platform": "Windows", "os_version": "11 24H2",
            "connection_type": "Wi-Fi", "manufacturer": "Lenovo", "model": "T14", "notes": "Office device",
        }

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_create_activate_edit_and_delete_profile(self):
        created = self.client.post("/api/device-profiles", json=self.payload)
        self.assertEqual(created.status_code, 201)
        profile = created.get_json()["profile"]
        with self.client.session_transaction() as session:
            self.assertEqual(session["active_device_profile_id"], profile["id"])

        page = self.client.get("/device-profiles").get_data(as_text=True)
        self.assertIn("Work laptop", page)
        self.assertIn("Windows 11 24H2", page)

        updated = self.client.patch(f"/api/device-profiles/{profile['id']}", json={**self.payload, "connection_type": "VPN"})
        self.assertEqual(updated.get_json()["profile"]["connection_type"], "VPN")
        self.assertEqual(self.client.delete(f"/api/device-profiles/{profile['id']}").status_code, 200)
        self.assertEqual(self.service.list(), [])

    def test_active_profile_survives_home_and_appears_in_wizard_and_search(self):
        profile = self.client.post("/api/device-profiles", json=self.payload).get_json()["profile"]
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("Device: Work laptop", home)
        wizard = self.client.get("/wizard?workflow=internet").get_data(as_text=True)
        self.assertIn("Troubleshooting <strong>Work laptop</strong>", wizard)
        search = self.client.get("/search?q=network").get_data(as_text=True)
        self.assertIn("Results are prioritized for <strong>Work laptop</strong>", search)

    def test_invalid_profile_is_rejected(self):
        response = self.client.post("/api/device-profiles", json={**self.payload, "platform": "Amiga"})
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(DeviceProfileError):
            self.service.get("../unsafe")


if __name__ == "__main__":
    unittest.main()
