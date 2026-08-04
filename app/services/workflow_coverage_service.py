import re
from datetime import datetime, timezone

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator


class WorkflowCoverageError(ValueError):
    pass


class WorkflowCoverageService:
    SUPPORTED_TYPES = {"question", "instruction", "resolution", "transition"}

    def generate_help_text(self, node):
        if not isinstance(node, dict) or node.get("type") not in self.SUPPORTED_TYPES:
            raise WorkflowCoverageError("Choose a supported workflow node.")
        node_type = node["type"]
        subject = self._subject(node)
        if node_type == "question":
            return (
                f"Use what you can directly observe about {subject.lower()} rather than guessing. "
                "If you are unsure, choose the safest uncertainty option so the workflow can gather more evidence."
            )
        if node_type == "instruction":
            return (
                f"This step checks whether {subject.lower()} is contributing to the problem. "
                "Complete only the described action, note what changes, and avoid changing unrelated settings."
            )
        if node_type == "resolution":
            return (
                f"This outcome records that the diagnostic path ended at {subject.lower()}. "
                "Document what was observed and monitor whether the issue returns."
            )
        return (
            f"This step moves the investigation to {subject.lower()} because the current checks did not fully explain the issue."
        )

    def create_article_draft(self, workflow, node_id, node):
        if not isinstance(workflow, dict) or not isinstance(node, dict):
            raise WorkflowCoverageError("Workflow and node data are required.")
        if node.get("type") != "instruction":
            raise WorkflowCoverageError("Knowledge articles can be drafted from instructional nodes.")
        title = str(node.get("title") or node_id.replace("_", " ").title()).strip()
        instruction = str(node.get("instruction") or "Follow the workflow instruction and record the result.").strip()
        workflow_id = str(workflow.get("workflow_id") or "workflow")
        article_id = self._slug(f"{workflow_id}-{node_id}")
        article = create_article_template()
        article.update({
            "id": article_id,
            "title": f"How to {title}",
            "category": str(workflow.get("category") or "Troubleshooting"),
            "difficulty": "Beginner",
            "estimated_time": "5 to 10 minutes",
            "overview": (
                f"This draft supports the “{title}” step in the {workflow.get('name') or workflow_id} workflow. "
                "It explains the intended check, safe execution, and the evidence to capture before continuing."
            ),
            "checklist": [
                "Save open work and confirm you are working on the intended device.",
                instruction,
                "Record what changed and return to the workflow before making additional changes.",
            ],
            "common_indicators": [
                f"The workflow reached the {title} step.",
                "The observed result will determine which diagnostic step should come next.",
            ],
            "commands": [],
            "related_topics": [
                str(workflow.get("name") or "Troubleshooting workflow"),
                title,
                "Diagnostic reasoning",
            ],
            "quiz": [{
                "question": "What should you do after completing this troubleshooting step?",
                "answers": [
                    "Record the result and return to the workflow",
                    "Change several unrelated settings",
                    "Ignore what happened",
                ],
                "correct_answer": "Record the result and return to the workflow",
            }],
            "sources": [],
            "generation": {
                "provider": "Gnojo Coverage Assistant",
                "model": "deterministic-node-draft-v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "review": {
                "status": "draft",
                "reviewed_by": None,
                "reviewed_at": None,
                "notes": [
                    "Generated from a workflow node.",
                    "Verify technical accuracy and add authoritative sources before publication.",
                ],
            },
        })
        errors = ArticleValidator.validate(article)
        if errors:
            raise WorkflowCoverageError("The article draft could not be validated: " + errors[0])
        return article

    @staticmethod
    def _subject(node):
        return str(
            node.get("title") or node.get("question") or node.get("instruction")
            or node.get("message") or "this diagnostic result"
        ).strip().rstrip("?.")[:140]

    @staticmethod
    def _slug(value):
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:120] or "workflow-knowledge-draft"
