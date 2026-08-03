import json
from pathlib import Path


class WorkflowDraftService:
    """
    Save and load AI-generated workflow drafts.
    """

    def __init__(self):
        self.drafts_path = (
            Path(__file__).resolve().parent.parent
            / "workflow_drafts"
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

        filename = f"{workflow_id}.json"

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

    def list_drafts(self):
        """
        Return all saved workflow drafts.
        """

        drafts = []

        for file_path in sorted(
            self.drafts_path.glob("*.json")
        ):
            with open(
                file_path,
                "r",
                encoding="utf-8",
            ) as file:
                workflow = json.load(file)

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
                }
            )

        return drafts

    def get_draft(self, filename):
        """
        Load one saved workflow draft by filename.
        """

        file_path = self.drafts_path / filename

        if not file_path.exists():
            return None

        with open(
            file_path,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)