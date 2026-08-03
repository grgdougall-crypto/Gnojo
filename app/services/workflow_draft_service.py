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