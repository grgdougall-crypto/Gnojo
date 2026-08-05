import re
from datetime import datetime, timezone

from app.knowledge.article_schema import create_article_template
from app.knowledge.article_validator import ArticleValidator
from app.services.article_tag_service import ArticleTagService


class WorkflowCoverageError(ValueError):
    pass


class WorkflowCoverageService:
    SUPPORTED_TYPES = {"question", "instruction", "resolution", "transition"}

    INSTRUCTION_GUIDANCE = (
        (
            (r"\bnslookup\b", r"\bdns resolution\b"),
            "Record the DNS server used, any returned IP address, and errors such as a timeout or nonexistent domain. "
            "This lookup gathers name-resolution evidence without changing DNS settings.",
        ),
        (
            (r"\bping\b", r"\btest (?:the )?default gateway\b"),
            "Record whether the gateway replies, along with packet loss or timeout messages. "
            "A reply confirms local reachability only; it does not prove that internet access or name resolution is working.",
        ),
        (
            (r"\bipconfig\b",),
            "Record the active adapter's IPv4 address, default gateway, DNS servers, and DHCP status. "
            "The display-only ipconfig commands gather configuration evidence without changing network settings.",
        ),
        (
            (r"\bprinter\b", r"\bpaper jam\b", r"\btoner\b"),
            "Record the printer's exact status, warning light, or error code before clearing it. "
            "Check paper, ink or toner, covers, and jams, then use manufacturer guidance for unfamiliar codes.",
        ),
        (
            (r"\bmonitor\b", r"\bexternal display\b", r"\bwindows key \+ p\b"),
            "Record whether Windows detects the display and which projection mode is selected. "
            "Also note the cable, adapter, and ports tested so a settings problem can be separated from a hardware problem.",
        ),
        (
            (r"\bstartup app", r"\btask manager\b"),
            "Record which startup applications are enabled and their reported startup impact. "
            "Do not disable security, accessibility, management, or unfamiliar software without approval.",
        ),
        (
            (r"\bcable\b", r"\bconnector\b", r"\bphysical connection\b"),
            "Check both ends of the connection and record the cable, adapter, and ports tested. "
            "Change one connection at a time so the result identifies which component affected the symptom.",
        ),
        (
            (r"\bpower\b", r"\bstatus light\b"),
            "Record the device's power state, display message, and status-light pattern before restarting or disconnecting it. "
            "Use only controls and power sources you are authorized to operate.",
        ),
    )

    def generate_help_text(self, node):
        if not isinstance(node, dict) or node.get("type") not in self.SUPPORTED_TYPES:
            raise WorkflowCoverageError("Choose a supported workflow node.")
        node_type = node["type"]
        subject = self._subject(node)
        if node_type == "question":
            return (
                f"Use direct evidence to answer: {subject}. "
                "If you are unsure, choose the safest uncertainty option so the workflow can gather more evidence."
            )
        if node_type == "instruction":
            return self._instruction_help(node, subject)
        if node_type == "resolution":
            message = str(node.get("message") or "").strip()
            return (
                f"This result means: {message or subject}. "
                "Record the checks that led here and monitor whether the symptom returns."
            )
        message = str(node.get("message") or "").strip()
        return (
            f"Continue to {subject} because the current checks did not fully explain the issue. "
            f"{message}".strip()
        )

    def _instruction_help(self, node, subject):
        instruction = str(node.get("instruction") or "").strip()
        searchable = " ".join((subject, instruction)).lower()

        for patterns, guidance in self.INSTRUCTION_GUIDANCE:
            if any(re.search(pattern, searchable) for pattern in patterns):
                return f"Use this step to {subject.lower()}. {guidance}"

        action = instruction or subject
        return (
            f"Use this step to {subject.lower()}: {action} "
            "Record the specific result before continuing, and avoid changing unrelated settings."
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
        article["tags"] = ArticleTagService.generate(article)
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
