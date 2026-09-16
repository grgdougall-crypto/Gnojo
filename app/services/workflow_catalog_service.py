import json
import re
from copy import deepcopy
from pathlib import Path

from app.services.workflow_metadata_service import workflow_category, workflow_platform
from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)


class WorkflowCatalogError(WorkflowPublicationError):
    """Raised when workflow discovery cannot resolve one safe public catalog."""


class WorkflowCatalogService:
    """Resolve tracked built-ins and active publications into one public catalog."""

    WORKFLOW_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")

    def __init__(
        self,
        built_in_path=None,
        publications=None,
        built_in_metadata=None,
    ):
        self.built_in_path = Path(built_in_path or (
            Path(__file__).resolve().parent.parent / "decision_trees"
        ))
        self.publications = publications or WorkflowPublicationService()
        self.built_in_metadata = dict(built_in_metadata or {})

    def catalog(self):
        """Return one deterministically ordered entry per canonical workflow ID."""
        selected, _ = self._resolve()
        return {
            workflow_id: deepcopy(entry)
            for workflow_id, entry in self._ordered_items(selected)
        }

    def selected_workflows(self):
        """Return catalog metadata with its selected workflow content for internal reads."""
        selected, workflows = self._resolve()
        return [
            (deepcopy(entry), deepcopy(workflows[workflow_id]))
            for workflow_id, entry in self._ordered_items(selected)
        ]

    def built_ins(self):
        """Return tracked built-in authoring sources without consulting editable drafts."""
        entries, _ = self._read_built_ins()
        return [deepcopy(entry) for _, entry in self._ordered_items(entries)]

    def load_builtin(self, workflow_id):
        entries, workflows = self._read_built_ins()
        if workflow_id not in entries:
            return None
        return deepcopy(workflows[workflow_id])

    def _resolve(self):
        selected, workflows = self._read_built_ins()
        try:
            snapshots = self.publications.list_current(strict=True)
        except WorkflowPublicationError:
            raise

        for snapshot in snapshots:
            workflow = snapshot.get("workflow")
            publication = snapshot.get("publication") or {}
            workflow_id = self._validate_workflow(workflow, source="published workflow")
            entry = self._entry(
                workflow,
                source="published",
                version=publication.get("version"),
            )
            selected[workflow_id] = entry
            workflows[workflow_id] = workflow
        return selected, workflows

    def _read_built_ins(self):
        entries = {}
        workflows = {}
        if not self.built_in_path.exists():
            return entries, workflows
        if not self.built_in_path.is_dir():
            raise WorkflowCatalogError("Tracked workflow storage is invalid.")
        try:
            paths = sorted(self.built_in_path.glob("*.json"))
        except OSError as error:
            raise WorkflowCatalogError(
                "Tracked workflow storage could not be read safely."
            ) from error

        for path in paths:
            try:
                workflow = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise WorkflowCatalogError(
                    f"Tracked workflow '{path.name}' could not be read safely."
                ) from error
            workflow_id = self._validate_workflow(
                workflow, source=f"tracked workflow '{path.name}'"
            )
            if path.stem != workflow_id:
                raise WorkflowCatalogError(
                    f"Tracked workflow identity does not match filename '{path.name}'."
                )
            if workflow_id in entries:
                raise WorkflowCatalogError(
                    f"Tracked workflow identity '{workflow_id}' is ambiguous."
                )
            entries[workflow_id] = self._entry(workflow, source="built_in")
            workflows[workflow_id] = workflow
        return entries, workflows

    def _entry(self, workflow, *, source, version=None):
        workflow_id = workflow["workflow_id"]
        presentation = self.built_in_metadata.get(workflow_id, {})
        name = workflow.get("name")
        description = workflow.get("description")
        return {
            "workflow_id": workflow_id,
            "name": (
                name.strip()
                if isinstance(name, str) and name.strip()
                else presentation.get("name") or workflow_id.replace("_", " ").title()
            ),
            "description": (
                description.strip()
                if isinstance(description, str) and description.strip()
                else presentation.get("description")
                or "Follow this guided troubleshooting workflow."
            ),
            "icon": workflow.get("icon") or presentation.get("icon") or "bi-signpost-split",
            "category": workflow_category(workflow),
            "platform": workflow_platform(workflow),
            "estimated_steps": workflow.get("estimated_steps"),
            "progress_mode": (
                "branch_aware"
                if workflow.get("progress_mode") == "branch_aware"
                else "static"
            ),
            "source": source,
            "version": version if source == "published" else None,
        }

    def _validate_workflow(self, workflow, *, source):
        if not isinstance(workflow, dict):
            raise WorkflowCatalogError(f"The {source} is invalid.")
        workflow_id = workflow.get("workflow_id")
        if (
            not isinstance(workflow_id, str)
            or self.WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None
        ):
            raise WorkflowCatalogError(f"The {source} has an invalid identity.")
        nodes = workflow.get("nodes")
        start_node = workflow.get("start_node")
        if not isinstance(nodes, dict) or not nodes or start_node not in nodes:
            raise WorkflowCatalogError(f"The {source} has an invalid workflow graph.")
        return workflow_id

    @staticmethod
    def _ordered_items(entries):
        return sorted(
            entries.items(),
            key=lambda item: (item[1]["name"].casefold(), item[0]),
        )
