import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.services.workflow_validation_service import WorkflowValidationService


class WorkflowPublicationError(Exception):
    """Raised when a workflow cannot be safely published."""


class WorkflowPublicationService:
    """Create immutable, numbered workflow publication snapshots."""

    def __init__(self, publication_path=None):
        self.publication_path = Path(publication_path) if publication_path else (
            Path(__file__).resolve().parent.parent / "workflow_publications"
        )
        self.publication_path.mkdir(parents=True, exist_ok=True)

    def status(self, workflow_id):
        directory = self._workflow_directory(workflow_id)
        versions = []

        for path in sorted(directory.glob("v*.json"), reverse=True):
            match = re.fullmatch(r"v(\d{4})\.json", path.name)
            if not match:
                continue
            snapshot = self._load_json(path)
            publication = snapshot.get("publication", {})
            versions.append(
                {
                    "version": int(match.group(1)),
                    "label": publication.get("label", f"Version {int(match.group(1))}"),
                    "published_at": publication.get("published_at"),
                    "source_filename": publication.get("source_filename"),
                    "content_hash": publication.get("content_hash"),
                }
            )

        return {
            "is_published": bool(versions),
            "current_version": versions[0]["version"] if versions else None,
            "versions": versions,
        }

    def list_current(self):
        """Return the active immutable snapshot for every published workflow."""
        published = []
        if not self.publication_path.exists():
            return published
        for directory in sorted(self.publication_path.iterdir()):
            if not directory.is_dir():
                continue
            try:
                snapshot = self.load_current(directory.name)
            except WorkflowPublicationError:
                continue
            if snapshot:
                published.append(snapshot)
        return published

    def load_current(self, workflow_id):
        """Load the version selected by a workflow's current manifest."""
        directory = self._workflow_directory(workflow_id, create=False)
        manifest_path = directory / "current.json"
        if not manifest_path.is_file():
            return None
        manifest = self._load_json(manifest_path)
        version = manifest.get("current_version")
        if not isinstance(version, int) or version < 1:
            raise WorkflowPublicationError("Published workflow manifest is invalid.")
        return self.load_version(workflow_id, version)

    def load_version(self, workflow_id, version):
        """Load one immutable published version by number."""
        if not isinstance(version, int) or version < 1:
            raise WorkflowPublicationError("Published workflow version is invalid.")
        directory = self._workflow_directory(workflow_id, create=False)
        version_path = directory / f"v{version:04d}.json"
        if not version_path.is_file():
            raise WorkflowPublicationError("Published workflow version is missing.")
        snapshot = self._load_json(version_path)
        workflow = snapshot.get("workflow")
        if not isinstance(workflow, dict):
            raise WorkflowPublicationError("Published workflow snapshot is invalid.")
        return snapshot

    def publish(self, workflow, source_filename, label=None):
        validation = WorkflowValidationService().validate(workflow)
        if not validation["is_valid"]:
            raise WorkflowPublicationError(
                "Workflow must pass validation before publishing."
            )

        workflow_id = workflow.get("workflow_id")
        directory = self._workflow_directory(workflow_id)
        status = self.status(workflow_id)
        content_hash = self.content_hash(workflow)
        if status["versions"] and status["versions"][0]["content_hash"] == content_hash:
            raise WorkflowPublicationError(
                "This draft is identical to the currently published version."
            )
        version = (status["current_version"] or 0) + 1
        published_at = datetime.now(timezone.utc).isoformat()
        snapshot_workflow = deepcopy(workflow)
        snapshot = {
            "publication": {
                "version": version,
                "label": (label or "").strip() or f"Version {version}",
                "published_at": published_at,
                "source_filename": source_filename,
                "content_hash": content_hash,
            },
            "workflow": snapshot_workflow,
        }
        version_path = directory / f"v{version:04d}.json"

        try:
            with version_path.open("x", encoding="utf-8") as file:
                json.dump(snapshot, file, indent=4)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError as error:
            raise WorkflowPublicationError(
                "A publication was created at the same time. Please try again."
            ) from error

        self._write_current_manifest(
            directory,
            {
                "workflow_id": workflow_id,
                "current_version": version,
                "published_at": published_at,
                "content_hash": content_hash,
            },
        )
        return self.status(workflow_id)

    def _workflow_directory(self, workflow_id, create=True):
        if not isinstance(workflow_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", workflow_id):
            raise WorkflowPublicationError("Workflow ID is not safe to publish.")
        directory = self.publication_path / workflow_id
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def content_hash(self, workflow):
        canonical = json.dumps(
            workflow,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _load_json(self, path):
        try:
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowPublicationError("Published workflow data is damaged and could not be loaded safely.") from error
        if not isinstance(value, dict):
            raise WorkflowPublicationError("Published workflow data is invalid.")
        return value

    def _write_current_manifest(self, directory, manifest):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".current-",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(manifest, file, indent=4)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, directory / "current.json")
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
