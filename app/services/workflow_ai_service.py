import json

from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider


class WorkflowAIError(Exception):
    """Raised when a safe workflow-node suggestion cannot be produced."""


class WorkflowAIService:
    styles = {
        "clarity": "Improve clarity while preserving the meaning and level of detail.",
        "concise": "Make the writing shorter and more direct without losing necessary instructions.",
        "technician": "Rewrite for an IT help-desk technician using precise, practical language.",
        "end_user": "Rewrite for a non-technical end user using calm, plain language.",
    }

    editable_fields = {
        "question": {"question", "help_text", "answer_labels"},
        "instruction": {"title", "instruction", "help_text"},
        "resolution": {"title", "message", "help_text"},
        "transition": {"title", "message", "help_text"},
    }

    def __init__(self, providers=None):
        self.providers = providers

    def improve_node(self, node_id, node, style):
        if style not in self.styles:
            raise WorkflowAIError("Unknown improvement style.")
        if not isinstance(node, dict) or node.get("type") not in self.editable_fields:
            raise WorkflowAIError("This node type cannot be improved.")

        original = self._editable_content(node)
        prompt = self._build_prompt(node_id, node.get("type"), original, style)
        generated, provider_name = self._generate(prompt)
        proposed = self._sanitize(node, generated)

        if proposed == original:
            raise WorkflowAIError("The AI did not produce a meaningful change.")

        return {
            "style": style,
            "provider": provider_name,
            "original": original,
            "proposed": proposed,
        }

    def _providers(self):
        if self.providers is not None:
            return self.providers
        return [
            ("Gemini", GeminiProvider),
            ("OpenAI", OpenAIProvider),
        ]

    def _generate(self, prompt):
        errors = []
        for provider_name, provider_source in self._providers():
            try:
                provider = provider_source() if isinstance(provider_source, type) else provider_source
                result = provider.generate_workflow_node_suggestion(prompt)
                if not isinstance(result, dict):
                    raise RuntimeError("Provider returned an unexpected response.")
                return result, provider_name
            except Exception as error:
                errors.append(str(error))
        raise WorkflowAIError(
            "AI assistance is temporarily unavailable. "
            + (errors[-1] if errors else "No provider is configured.")
        )

    def _editable_content(self, node):
        node_type = node["type"]
        content = {}
        for field in self.editable_fields[node_type] - {"answer_labels"}:
            content[field] = node.get(field, "") or ""
        if node_type == "question":
            answers = node.get("answers") or {}
            content["answer_labels"] = {
                answer_id: (
                    answer.get("label", answer_id)
                    if isinstance(answer, dict)
                    else answer_id
                )
                for answer_id, answer in answers.items()
            } if isinstance(answers, dict) else {}
        return content

    def _sanitize(self, node, generated):
        original = self._editable_content(node)
        allowed = self.editable_fields[node["type"]]
        proposed = dict(original)

        for field in allowed - {"answer_labels"}:
            value = generated.get(field)
            if isinstance(value, str) and value.strip():
                proposed[field] = value.strip()

        if "answer_labels" in allowed:
            labels = generated.get("answer_labels")
            if isinstance(labels, dict):
                proposed["answer_labels"] = {
                    answer_id: (
                        labels.get(answer_id).strip()
                        if isinstance(labels.get(answer_id), str) and labels[answer_id].strip()
                        else original_label
                    )
                    for answer_id, original_label in original["answer_labels"].items()
                }
        return proposed

    def _build_prompt(self, node_id, node_type, original, style):
        return f"""
You are improving one Gnojo troubleshooting workflow node.

Goal: {self.styles[style]}

Security and structure rules:
- Return only a JSON object.
- Use only the keys already present in EDITABLE_CONTENT.
- Never add or change node IDs, node types, routes, next-node values, answer IDs, commands, URLs, or workflow IDs.
- Preserve the technical meaning and do not invent troubleshooting facts.
- For answer_labels, keep every existing key exactly unchanged and rewrite values only.
- Empty optional fields may remain empty.

NODE_ID: {node_id}
NODE_TYPE: {node_type}
EDITABLE_CONTENT:
{json.dumps(original, indent=2)}
""".strip()
