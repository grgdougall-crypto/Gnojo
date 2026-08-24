from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.curator_resolution_service import CuratorResolutionService
from curator.resolution import ResolutionPackageError


class CuratorBatchService:
    def __init__(self, repository_root: Path | None = None):
        self.root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        self.service = CuratorResolutionService(self.root)
        self.lock_path = self.root / ".curator-resolution-batch.lock"

    def prepare_first_batch(self) -> dict[str, Any]:
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise ResolutionPackageError("An assisted-resolution batch is already running.") from error
        os.close(descriptor)
        try:
            tasks = self.service.eligible_tasks()
            selected = tasks[:10]
            prepared, failed = [], []
            for task in selected:
                try:
                    package = self.service.prepare(task["task_id"])
                    prepared.append({"task_id": task["task_id"], "recommendation": package["recommendation"], "version": package["version"]})
                except Exception as error:
                    failed.append({"task_id": task["task_id"], "error": str(error)})
            result = {"at": datetime.now(timezone.utc).isoformat(), "eligible": len(tasks), "selected": len(selected), "prepared": prepared, "failed": failed}
            batch_root = self.root / "curation_memory" / "resolution_batches"
            batch_root.mkdir(parents=True, exist_ok=True)
            (batch_root / "latest.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return result
        finally:
            self.lock_path.unlink(missing_ok=True)

    def latest(self) -> dict[str, Any]:
        path = self.root / "curation_memory" / "resolution_batches" / "latest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            available, unavailable = [], []
            for item in value.get("prepared", []):
                if isinstance(item, dict) and self.service.get(str(item.get("task_id") or "")):
                    available.append(item)
                else:
                    unavailable.append(item)
            return {**value, "prepared": available, "unavailable": unavailable}
        except (OSError, json.JSONDecodeError):
            return {}
