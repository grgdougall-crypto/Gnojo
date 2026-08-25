import json
import os
from pathlib import Path
import tempfile
from copy import deepcopy

from app.services.workflow_metadata_service import workflow_category, workflow_platform


class WorkflowDraftError(Exception):
    """Raised when a workflow draft cannot be safely updated."""


class WorkflowDraftService:
    """
    Save and load AI-generated workflow drafts.
    """

    def __init__(self, drafts_path=None):
        self.drafts_path = Path(drafts_path) if drafts_path else (
            Path(__file__).resolve().parent.parent / "workflow_drafts"
        )

        self.drafts_path.mkdir(
            exist_ok=True
        )

    def save_draft(
        self,
        workflow,
    ):
        """
        Save a workflow draft to disk.

        Returns the filename.
        """

        workflow_id = workflow.get(
            "workflow_id",
            "untitled_workflow",
        )

        filename = self.filename_for(workflow_id)

        file_path = (
            self.drafts_path
            / filename
        )

        with open(
            file_path,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                workflow,
                file,
                indent=4,
            )

        return filename

    @staticmethod
    def filename_for(workflow_id):
        filename = f"{workflow_id}.json"
        if (not isinstance(workflow_id, str) or not workflow_id.strip()
                or Path(filename).name != filename):
            raise WorkflowDraftError("The workflow ID cannot form a safe draft filename.")
        return filename

    def ensure_editable_copy(self, workflow_id, workflow, *, source_type):
        """Return one validated editable draft without mutating its source lifecycle."""
        for item in self.list_drafts():
            if item.get("workflow_id") == workflow_id and not item.get("is_damaged"):
                return item["filename"]
        if not isinstance(workflow, dict) or workflow.get("workflow_id") != workflow_id:
            raise WorkflowDraftError("The canonical workflow identity is invalid.")
        from app.services.workflow_validation_service import WorkflowValidationService
        editable = deepcopy(workflow)
        editable["status"] = "Editable Copy"
        editable["draft_origin"] = {"type": source_type, "workflow_id": workflow_id}
        validation = WorkflowValidationService().validate(editable)
        if not validation["is_valid"]:
            raise WorkflowDraftError(
                "The workflow must pass validation before an editable copy can be created."
            )
        return self.save_draft(editable)

    def list_drafts(self):
        """
        Return all saved workflow drafts.
        """

        drafts = []

        for file_path in sorted(
            self.drafts_path.glob("*.json")
        ):
            try:
                with file_path.open("r", encoding="utf-8") as file:
                    workflow = json.load(file)
                if not isinstance(workflow, dict):
                    raise ValueError("Workflow must be an object")
            except (OSError, json.JSONDecodeError, ValueError):
                drafts.append({
                    "filename": file_path.name,
                    "workflow_id": None,
                    "name": f"Damaged workflow: {file_path.stem}",
                    "estimated_steps": None,
                    "progress_mode": None,
                    "category": "Needs attention",
                    "platform": "Unknown",
                    "is_damaged": True,
                })
                continue

            drafts.append(
                {
                    "filename": file_path.name,
                    "workflow_id": workflow.get(
                        "workflow_id",
                    ),
                    "name": workflow.get(
                        "name",
                    ),
                    "estimated_steps": workflow.get(
                        "estimated_steps",
                    ),
                    "progress_mode": (
                        "branch_aware"
                        if workflow.get("progress_mode") == "branch_aware"
                        else "static"
                    ),
                    "category": workflow_category(workflow),
                    "platform": workflow_platform(workflow),
                }
            )

        return drafts

    def get_draft(self, filename):
        """
        Load one saved workflow draft by filename.
        """

        if not filename or Path(filename).name != filename:
            raise WorkflowDraftError("Workflow filename is invalid.")
        file_path = self.drafts_path / filename
        if not file_path.exists():
            return None
        try:
            with file_path.open("r", encoding="utf-8") as file:
                workflow = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowDraftError("This workflow draft is damaged and could not be opened safely.") from error
        if not isinstance(workflow, dict):
            raise WorkflowDraftError("This workflow draft does not contain valid workflow data.")
        return workflow

    def update_node(self, filename, node_id, changes):
        """Update one node in a draft and atomically persist the JSON file."""

        if not filename or Path(filename).name != filename:
            raise WorkflowDraftError("Invalid workflow filename.")

        if not isinstance(node_id, str) or not node_id.strip():
            raise WorkflowDraftError("A node ID is required.")

        if not isinstance(changes, dict):
            raise WorkflowDraftError("Node changes must be an object.")

        file_path = self.drafts_path / filename
        if not file_path.is_file():
            raise FileNotFoundError(filename)

        with file_path.open("r", encoding="utf-8") as file:
            workflow = json.load(file)

        nodes = workflow.get("nodes")
        if not isinstance(nodes, dict) or node_id not in nodes:
            raise KeyError(node_id)

        allowed_fields = {
            "title",
            "question",
            "instruction",
            "message",
            "help_text",
            "knowledge_article",
            "next",
            "next_workflow",
            "button_label",
            "answers",
            "conditions",
            "skip_to",
        }
        unexpected = set(changes) - allowed_fields
        if unexpected:
            raise WorkflowDraftError(
                f"Unsupported node field: {sorted(unexpected)[0]}"
            )

        node = nodes[node_id]
        for field, value in changes.items():
            if field == "conditions":
                from app.services.workflow_condition_service import CONDITION_FIELDS
                if not isinstance(value, dict):
                    raise WorkflowDraftError("Node conditions must be an object.")
                unexpected_conditions = set(value) - set(CONDITION_FIELDS)
                if unexpected_conditions:
                    raise WorkflowDraftError(f"Unsupported condition: {sorted(unexpected_conditions)[0]}")
                normalized_conditions = {}
                for condition_field, condition_value in value.items():
                    if condition_value and condition_value not in CONDITION_FIELDS[condition_field]:
                        raise WorkflowDraftError(f"Choose a valid {condition_field.replace('_', ' ')} condition.")
                    if condition_value:
                        normalized_conditions[condition_field] = condition_value
                if normalized_conditions:
                    node[field] = normalized_conditions
                else:
                    node.pop(field, None)
                continue
            if field == "answers":
                if not isinstance(value, dict):
                    raise WorkflowDraftError("Answers must be an object.")
                for answer_id, answer in value.items():
                    if not isinstance(answer_id, str) or not answer_id.strip():
                        raise WorkflowDraftError("Each answer needs an ID.")
                    if not isinstance(answer, dict):
                        raise WorkflowDraftError("Each answer must be an object.")
                    if not isinstance(answer.get("label"), str) or not answer["label"].strip():
                        raise WorkflowDraftError("Each answer needs a label.")
                    if not isinstance(answer.get("next"), str) or not answer["next"].strip():
                        raise WorkflowDraftError("Each answer needs a route.")
                node[field] = value
                continue

            if value is not None and not isinstance(value, str):
                raise WorkflowDraftError(f"{field} must be text.")

            normalized = (value or "").strip()
            if normalized:
                node[field] = normalized
            else:
                node.pop(field, None)

        self._write_atomic(file_path, workflow)
        return workflow

    def update_settings(self, filename, changes):
        """Validate and persist editable workflow-level settings."""

        if not filename or Path(filename).name != filename:
            raise WorkflowDraftError("Invalid workflow filename.")
        if not isinstance(changes, dict):
            raise WorkflowDraftError("Workflow settings must be an object.")

        allowed_fields = {"name", "description", "estimated_steps", "start_node", "category", "platform"}
        unexpected = set(changes) - allowed_fields
        if unexpected:
            raise WorkflowDraftError(
                f"Unsupported workflow setting: {sorted(unexpected)[0]}"
            )

        file_path = self.drafts_path / filename
        if not file_path.is_file():
            raise FileNotFoundError(filename)
        with file_path.open("r", encoding="utf-8") as file:
            workflow = json.load(file)

        name = changes.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WorkflowDraftError("Workflow name is required.")

        description = changes.get("description", "")
        if not isinstance(description, str):
            raise WorkflowDraftError("Description must be text.")

        category = changes.get("category", workflow_category(workflow))
        allowed_categories = {"Desktop Support", "Networking", "Servers & Identity", "Security", "Printers", "Other"}
        if category not in allowed_categories:
            raise WorkflowDraftError("Choose a valid workflow category.")

        platform = changes.get("platform", workflow_platform(workflow))
        allowed_platforms = {"Windows", "macOS", "Linux", "Cross-platform"}
        if platform not in allowed_platforms:
            raise WorkflowDraftError("Choose a valid workflow platform.")

        estimated_steps = changes.get("estimated_steps")
        if (
            isinstance(estimated_steps, bool)
            or not isinstance(estimated_steps, int)
            or not 1 <= estimated_steps <= 999
        ):
            raise WorkflowDraftError("Estimated steps must be between 1 and 999.")

        start_node = changes.get("start_node")
        nodes = workflow.get("nodes")
        if not isinstance(start_node, str) or not isinstance(nodes, dict) or start_node not in nodes:
            raise WorkflowDraftError("Start node must reference an existing node.")

        workflow["name"] = name.strip()
        workflow["estimated_steps"] = estimated_steps
        workflow["start_node"] = start_node
        workflow["category"] = category
        workflow["platform"] = platform
        if description.strip():
            workflow["description"] = description.strip()
        else:
            workflow.pop("description", None)

        self._write_atomic(file_path, workflow)
        return workflow

    def _write_atomic(self, file_path, workflow):
        """Write JSON beside the draft, then replace it in one operation."""

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{file_path.stem}-",
            suffix=".tmp",
            dir=self.drafts_path,
            text=True,
        )

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(workflow, file, indent=4)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, file_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
