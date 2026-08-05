import json
import re

from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider
from app.services.workflow_coverage_service import WorkflowCoverageService


class WorkflowHelpTextError(ValueError):
    """Raised when a safe, useful help-text suggestion cannot be accepted."""


class WorkflowHelpTextService:
    """Generate and validate reviewable workflow help-text suggestions."""

    GENERIC_PHRASES = (
        "this step checks whether",
        "complete only the described action",
        "note what changes",
        "follow the instructions",
        "this narrows the problem space",
    )
    KNOWN_COMMANDS = {
        "arp", "dism", "get-netadapter", "get-netipconfiguration", "hostname",
        "ipconfig", "netsh", "netstat", "nslookup", "ping", "powershell",
        "sfc", "systeminfo", "tasklist", "tracert", "whoami",
    }
    GUARDED_TECHNICAL_TERMS = {
        "dhcp": r"\bdhcp\b",
        "dns": r"\bdns\b",
        "ipv4": r"\bipv4\b",
        "ipv6": r"\bipv6\b",
        "subnet mask": r"\bsubnet mask\b",
    }
    STOP_WORDS = {
        "about", "after", "again", "also", "and", "before", "check", "does",
        "from", "have", "into", "only", "open", "that", "the", "then", "this",
        "through", "using", "what", "when", "where", "whether", "with", "your",
    }

    def __init__(self, providers=None, fallback=None):
        self.providers = providers
        self.fallback = fallback or WorkflowCoverageService()

    def suggest(self, workflow, node_id, node):
        self._validate_inputs(workflow, node_id, node)
        context = self._context(workflow, node_id, node)
        prompt = self._prompt(context)
        provider_errors = []

        for provider_name, provider_source in self._providers():
            try:
                provider = provider_source() if isinstance(provider_source, type) else provider_source
                generated = provider.generate_workflow_node_suggestion(prompt)
                if not isinstance(generated, dict):
                    raise WorkflowHelpTextError("The provider returned an unexpected response.")
                help_text = str(generated.get("help_text") or "").strip()
                self.validate_candidate(node, help_text)
                return {
                    "help_text": help_text,
                    "provider": provider_name,
                    "used_fallback": False,
                    "quality_checks": self._quality_checks(node, help_text),
                }
            except Exception as error:
                provider_errors.append(f"{provider_name}: {error}")

        help_text = self.fallback.generate_help_text(node)
        self.validate_candidate(node, help_text)
        return {
            "help_text": help_text,
            "provider": "Local fallback",
            "used_fallback": True,
            "quality_checks": self._quality_checks(node, help_text),
            "provider_errors": provider_errors,
        }

    def validate_candidate(self, node, help_text):
        if not isinstance(node, dict):
            raise WorkflowHelpTextError("A workflow node is required.")
        if not isinstance(help_text, str) or not help_text.strip():
            raise WorkflowHelpTextError("Help text cannot be empty.")

        candidate = " ".join(help_text.split())
        normalized = candidate.lower()
        if len(candidate) < 80:
            raise WorkflowHelpTextError("Help text must include specific evidence and interpretation.")
        if len(candidate) > 700:
            raise WorkflowHelpTextError("Help text must be 700 characters or fewer.")
        if any(phrase in normalized for phrase in self.GENERIC_PHRASES):
            raise WorkflowHelpTextError("Help text is too generic for this node.")
        if "http://" in normalized or "https://" in normalized:
            raise WorkflowHelpTextError("Help text cannot introduce external URLs.")

        context_text = self._node_text(node).lower()
        invented_commands = {
            command for command in self.KNOWN_COMMANDS
            if re.search(rf"\b{re.escape(command)}\b", normalized)
            and not re.search(rf"\b{re.escape(command)}\b", context_text)
        }
        if invented_commands:
            raise WorkflowHelpTextError(
                "Help text introduced a command that is not present in the node: "
                + sorted(invented_commands)[0]
            )

        introduced_terms = [
            label for label, pattern in self.GUARDED_TECHNICAL_TERMS.items()
            if re.search(pattern, normalized) and not re.search(pattern, context_text)
        ]
        if introduced_terms:
            raise WorkflowHelpTextError(
                "Help text introduced technical evidence that is not present in the node: "
                + introduced_terms[0]
            )

        context_terms = self._meaningful_terms(context_text)
        if context_terms and not any(term in normalized for term in context_terms):
            raise WorkflowHelpTextError("Help text does not appear specific to the selected node.")
        return candidate

    def _providers(self):
        if self.providers is not None:
            return self.providers
        return (("Gemini", GeminiProvider), ("OpenAI", OpenAIProvider))

    def _context(self, workflow, node_id, node):
        nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), dict) else {}
        previous = []
        for candidate_id, candidate in nodes.items():
            if not isinstance(candidate, dict):
                continue
            destinations = [candidate.get("next"), candidate.get("skip_to")]
            answers = candidate.get("answers")
            if isinstance(answers, dict):
                destinations.extend(
                    answer.get("next") if isinstance(answer, dict) else answer
                    for answer in answers.values()
                )
            if node_id in destinations:
                previous.append(self._node_summary(candidate_id, candidate))

        destinations = []
        if node.get("next"):
            destinations.append(node["next"])
        answers = node.get("answers")
        if isinstance(answers, dict):
            destinations.extend(
                answer.get("next") if isinstance(answer, dict) else answer
                for answer in answers.values()
            )
        following = [
            self._node_summary(destination, nodes[destination])
            for destination in destinations
            if destination in nodes and isinstance(nodes[destination], dict)
        ]
        return {
            "workflow": {
                "name": workflow.get("name"),
                "description": workflow.get("description"),
                "category": workflow.get("category"),
                "platform": workflow.get("platform"),
                "difficulty": workflow.get("difficulty"),
            },
            "node": self._node_summary(node_id, node, include_content=True),
            "previous_nodes": previous[:4],
            "next_nodes": following[:6],
        }

    def _prompt(self, context):
        return f"""
You are writing one help-text field for a Gnojo troubleshooting workflow node.

Return only a JSON object with one key: "help_text".

Write 2 to 4 concise sentences that collectively:
- explain the purpose of this exact step;
- name the specific evidence the user should observe or record;
- explain what the result can and cannot establish when that distinction matters;
- state a relevant safety, authorization, or change-control boundary when applicable.

Trust rules:
- Ground every statement in the supplied workflow and node context.
- Treat neighboring nodes as routing context only. Never copy their commands, evidence fields, or instructions into the current node.
- The previous saved help text is intentionally excluded because it may be generic or incorrect.
- Do not invent commands, URLs, settings, products, symptoms, thresholds, or next steps.
- Do not merely repeat the title or instruction.
- Do not use generic filler such as "this step checks whether" or "complete only the described action."
- Use calm, plain language appropriate to the configured difficulty.
- Preserve technical capitalization such as DNS, DHCP, IP, IPv4, USB, VPN, Windows, macOS, and Linux.
- Help text must be between 80 and 700 characters.

CONTEXT:
{json.dumps(context, indent=2)}
""".strip()

    def _quality_checks(self, node, help_text):
        context = self._node_text(node).lower()
        normalized = help_text.lower()
        commands = sorted(
            command for command in self.KNOWN_COMMANDS
            if re.search(rf"\b{re.escape(command)}\b", normalized)
        )
        return [
            "Uses the selected node's workflow context",
            "Includes specific evidence or interpretation",
            "Introduces no external URLs",
            (
                "Uses only commands already present in the node"
                if commands and all(command in context for command in commands)
                else "Introduces no new commands"
            ),
        ]

    def _validate_inputs(self, workflow, node_id, node):
        if not isinstance(workflow, dict) or not isinstance(node, dict):
            raise WorkflowHelpTextError("Workflow and node context are required.")
        if not isinstance(node_id, str) or not node_id.strip():
            raise WorkflowHelpTextError("A node ID is required.")
        if node.get("type") not in WorkflowCoverageService.SUPPORTED_TYPES:
            raise WorkflowHelpTextError("Choose a supported workflow node.")

    def _node_summary(self, node_id, node, include_content=False):
        summary = {
            "id": node_id,
            "type": node.get("type"),
            "title": node.get("title") or node.get("question"),
        }
        if include_content:
            for field in (
                "question", "instruction", "message", "answers",
                "conditions", "knowledge_article", "next", "next_workflow", "skip_to",
            ):
                if node.get(field):
                    summary[field] = node[field]
        return summary

    def _node_text(self, node):
        values = [
            node.get("title"), node.get("question"), node.get("instruction"),
            node.get("message"), node.get("knowledge_article"),
        ]
        answers = node.get("answers")
        if isinstance(answers, dict):
            for answer_id, answer in answers.items():
                values.append(answer_id)
                if isinstance(answer, dict):
                    values.append(answer.get("label"))
        return " ".join(str(value) for value in values if value)

    def _meaningful_terms(self, text):
        return {
            word for word in re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", text)
            if word not in self.STOP_WORDS
        }
