import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class TroubleshootingHistoryError(ValueError):
    pass


class TroubleshootingHistoryService:
    STATUSES = {"active", "completed", "abandoned"}

    def __init__(self, history_path=None):
        self.history_path = (
            Path(history_path)
            if history_path
            else Path(__file__).resolve().parent.parent / "troubleshooting_history"
        )
        self.history_path.mkdir(parents=True, exist_ok=True)

    def start(self, workflow_id, workflow_name, node_id, version=None,
              device=None, learning_mode=False):
        now = self._now()
        record = {
            "id": uuid4().hex,
            "workflow_id": str(workflow_id),
            "workflow_name": str(workflow_name),
            "workflow_version": version,
            "status": "active",
            "outcome": None,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "current_node_id": str(node_id),
            "final_node_id": None,
            "steps": 1,
            "backtracks": 0,
            "transitions": 0,
            "learning_mode": bool(learning_mode),
            "device": self._device_snapshot(device),
            "path": [str(node_id)],
        }
        self._write(self._path(record["id"]), record, exclusive=True)
        return record

    def progress(self, history_id, node_id, *, action="advance",
                 workflow_id=None, workflow_name=None, version=None):
        record = self.get(history_id)
        if record is None or record.get("status") != "active":
            return record
        node_id = str(node_id)
        path = record.setdefault("path", [])
        if action == "back":
            record["backtracks"] = int(record.get("backtracks", 0)) + 1
            if len(path) > 1:
                path.pop()
        else:
            if not path or path[-1] != node_id:
                path.append(node_id)
            record["steps"] = max(int(record.get("steps", 1)), len(path))
            if action == "transition":
                record["transitions"] = int(record.get("transitions", 0)) + 1
        record["current_node_id"] = node_id
        if workflow_id:
            record["workflow_id"] = str(workflow_id)
        if workflow_name:
            record["workflow_name"] = str(workflow_name)
        if version is not None:
            record["workflow_version"] = version
        record["updated_at"] = self._now()
        self._write(self._path(history_id), record)
        return record

    def complete(self, history_id, node_id, outcome=None):
        record = self.get(history_id)
        if record is None:
            return None
        if record.get("status") == "completed":
            return record
        now = self._now()
        node_id = str(node_id)
        path = record.setdefault("path", [])
        if not path or path[-1] != node_id:
            path.append(node_id)
        record.update({
            "status": "completed",
            "outcome": (str(outcome).strip()[:160] if outcome else "Resolved"),
            "current_node_id": node_id,
            "final_node_id": node_id,
            "steps": max(int(record.get("steps", 1)), len(path)),
            "updated_at": now,
            "completed_at": now,
        })
        self._write(self._path(history_id), record)
        return record

    def abandon(self, history_id):
        record = self.get(history_id)
        if record is None or record.get("status") != "active":
            return record
        record["status"] = "abandoned"
        record["outcome"] = "Session ended before resolution"
        record["updated_at"] = self._now()
        self._write(self._path(history_id), record)
        return record

    def add_feedback(self, history_id, values):
        record = self.get(history_id)
        if record is None:
            raise FileNotFoundError(history_id)
        if record.get("status") != "completed":
            raise TroubleshootingHistoryError(
                "Feedback can be added after the workflow is completed."
            )
        if not isinstance(values, dict):
            raise TroubleshootingHistoryError("Feedback data is required.")
        solved = values.get("solved")
        if solved not in {"yes", "partially", "no"}:
            raise TroubleshootingHistoryError("Choose whether the workflow solved the problem.")
        try:
            clarity = int(values.get("clarity"))
        except (TypeError, ValueError) as error:
            raise TroubleshootingHistoryError("Choose a clarity rating.") from error
        if clarity not in {1, 2, 3, 4, 5}:
            raise TroubleshootingHistoryError("Clarity rating must be between 1 and 5.")
        confusing_step = values.get("confusing_step") or None
        if confusing_step is not None and confusing_step not in record.get("path", []):
            raise TroubleshootingHistoryError("Choose a step from this session.")
        comment = values.get("comment", "")
        if not isinstance(comment, str):
            raise TroubleshootingHistoryError("Feedback comment must be text.")
        record["feedback"] = {
            "solved": solved,
            "clarity": clarity,
            "confusing_step": confusing_step,
            "comment": comment.strip()[:500],
            "submitted_at": self._now(),
        }
        record["updated_at"] = self._now()
        self._write(self._path(history_id), record)
        return record["feedback"]

    def get(self, history_id):
        path = self._path(history_id)
        if not path.is_file():
            return None
        try:
            value = self._read(path)
        except (OSError, json.JSONDecodeError) as error:
            raise TroubleshootingHistoryError(
                "This troubleshooting history record is damaged."
            ) from error
        return value if isinstance(value, dict) else None

    def list(self, limit=100):
        records = []
        for path in self.history_path.glob("*.json"):
            try:
                value = self._read(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        records.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        return records[:max(1, min(int(limit), 500))]

    def analytics(self, records=None):
        records = list(records if records is not None else self.list(500))
        total = len(records)
        completed = sum(item.get("status") == "completed" for item in records)
        abandoned = sum(item.get("status") == "abandoned" for item in records)
        active = sum(item.get("status") == "active" for item in records)
        completed_steps = [int(item.get("steps", 0)) for item in records if item.get("status") == "completed"]
        feedback = [item["feedback"] for item in records if isinstance(item.get("feedback"), dict)]
        solved = sum(item.get("solved") == "yes" for item in feedback)
        partial = sum(item.get("solved") == "partially" for item in feedback)
        clarity_scores = [item.get("clarity") for item in feedback if isinstance(item.get("clarity"), int)]
        confusing_steps = {}
        for item in feedback:
            step = item.get("confusing_step")
            if step:
                confusing_steps[step] = confusing_steps.get(step, 0) + 1
        workflow_counts = {}
        for item in records:
            key = item.get("workflow_name") or item.get("workflow_id") or "Unknown"
            bucket = workflow_counts.setdefault(key, {"name": key, "sessions": 0, "completed": 0})
            bucket["sessions"] += 1
            bucket["completed"] += item.get("status") == "completed"
        popular = sorted(workflow_counts.values(), key=lambda item: (-item["sessions"], item["name"].lower()))[:5]
        for item in popular:
            item["completion_rate"] = round((item["completed"] / item["sessions"]) * 100) if item["sessions"] else 0
        return {
            "total": total,
            "completed": completed,
            "abandoned": abandoned,
            "active": active,
            "completion_rate": round((completed / total) * 100) if total else 0,
            "average_steps": round(sum(completed_steps) / len(completed_steps), 1) if completed_steps else 0,
            "feedback_count": len(feedback),
            "solved_rate": round((solved / len(feedback)) * 100) if feedback else 0,
            "partial_rate": round((partial / len(feedback)) * 100) if feedback else 0,
            "average_clarity": round(sum(clarity_scores) / len(clarity_scores), 1) if clarity_scores else 0,
            "confusing_steps": sorted(
                ({"node_id": key, "count": value} for key, value in confusing_steps.items()),
                key=lambda item: (-item["count"], item["node_id"]),
            )[:5],
            "popular_workflows": popular,
        }

    def delete(self, history_id):
        path = self._path(history_id)
        if not path.is_file():
            raise FileNotFoundError(history_id)
        path.unlink()

    def clear(self):
        for path in self.history_path.glob("*.json"):
            path.unlink()

    def _path(self, history_id):
        if not isinstance(history_id, str) or not re.fullmatch(r"[a-f0-9]{32}", history_id):
            raise TroubleshootingHistoryError("History record ID is invalid.")
        return self.history_path / f"{history_id}.json"

    @staticmethod
    def _device_snapshot(device):
        if not isinstance(device, dict):
            return None
        return {
            key: device.get(key)
            for key in ("id", "name", "platform", "device_type", "connection_type")
            if device.get(key)
        }

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

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
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".history-", suffix=".tmp", dir=self.history_path, text=True
        )
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
