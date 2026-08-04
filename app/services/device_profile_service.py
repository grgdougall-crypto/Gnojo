import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class DeviceProfileError(ValueError):
    pass


class DeviceProfileService:
    PLATFORMS = {"Windows", "macOS", "Linux", "ChromeOS", "Other"}
    DEVICE_TYPES = {"Desktop", "Laptop", "Server", "Tablet", "Virtual machine", "Other"}
    CONNECTION_TYPES = {"Ethernet", "Wi-Fi", "Cellular", "VPN", "Offline", "Other"}

    def __init__(self, profile_path=None):
        self.profile_path = Path(profile_path) if profile_path else Path(__file__).resolve().parent.parent / "device_profiles"
        self.profile_path.mkdir(parents=True, exist_ok=True)

    def list(self):
        profiles = []
        for path in sorted(self.profile_path.glob("*.json")):
            try:
                profiles.append(self._read(path))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(profiles, key=lambda item: item.get("name", "").lower())

    def get(self, profile_id):
        path = self._path(profile_id)
        if not path.is_file():
            return None
        try:
            value = self._read(path)
        except (OSError, json.JSONDecodeError) as error:
            raise DeviceProfileError("This device profile is damaged and could not be loaded safely.") from error
        if not isinstance(value, dict):
            raise DeviceProfileError("This device profile is invalid.")
        return value

    def create(self, values):
        profile = self._normalize(values)
        profile["id"] = uuid4().hex
        profile["created_at"] = datetime.now(timezone.utc).isoformat()
        self._write(self._path(profile["id"]), profile, exclusive=True)
        return profile

    def update(self, profile_id, values):
        path = self._path(profile_id)
        current = self.get(profile_id)
        if current is None:
            raise FileNotFoundError(profile_id)
        updated = self._normalize(values)
        updated.update({"id": profile_id, "created_at": current.get("created_at"), "updated_at": datetime.now(timezone.utc).isoformat()})
        self._write(path, updated)
        return updated

    def delete(self, profile_id):
        path = self._path(profile_id)
        if not path.is_file():
            raise FileNotFoundError(profile_id)
        path.unlink()

    def _normalize(self, values):
        if not isinstance(values, dict):
            raise DeviceProfileError("Device profile data is required.")
        result = {}
        for field, label, maximum in (
            ("name", "Profile name", 80), ("os_version", "OS version", 80),
            ("manufacturer", "Manufacturer", 80), ("model", "Model", 100), ("notes", "Notes", 500),
        ):
            value = values.get(field, "")
            if not isinstance(value, str):
                raise DeviceProfileError(f"{label} must be text.")
            result[field] = value.strip()[:maximum]
        if not result["name"]:
            raise DeviceProfileError("Profile name is required.")
        for field, label, allowed in (
            ("platform", "platform", self.PLATFORMS),
            ("device_type", "device type", self.DEVICE_TYPES),
            ("connection_type", "connection type", self.CONNECTION_TYPES),
        ):
            value = values.get(field)
            if value not in allowed:
                raise DeviceProfileError(f"Choose a valid {label}.")
            result[field] = value
        return result

    def _path(self, profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-f0-9]{32}", profile_id):
            raise DeviceProfileError("Device profile ID is invalid.")
        return self.profile_path / f"{profile_id}.json"

    @staticmethod
    def _read(path):
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _write(self, path, value, exclusive=False):
        if exclusive:
            with path.open("x", encoding="utf-8") as file:
                json.dump(value, file, indent=2)
                file.write("\n")
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".device-", suffix=".tmp", dir=self.profile_path, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(value, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
