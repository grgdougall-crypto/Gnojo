from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider


class WorkflowGenerationEngine:
    """
    Generate structured Gnojo workflow drafts.
    """

    SIZE_LIMITS = {
        "Small": (8, 12),
        "Medium": (14, 20),
        "Large": (22, 30),
    }

    def __init__(self):
        self.primary_provider = GeminiProvider()
        self.fallback_provider = OpenAIProvider()

    def generate_workflow(
        self,
        workflow_name,
        description="",
        platform="Windows",
        difficulty="Beginner",
        size="Medium",
    ):
        """
        Generate a workflow using Gemini first,
        then fall back to OpenAI if Gemini fails.
        """

        provider_name = "Gemini"

        try:
            generated = (
                self.primary_provider.generate_workflow(
                    workflow_name=workflow_name,
                    description=description,
                    platform=platform,
                    difficulty=difficulty,
                    size=size,
                )
            )
            self._enforce_size(generated, size)

        except Exception:
            provider_name = "OpenAI"

            generated = (
                self.fallback_provider.generate_workflow(
                    workflow_name=workflow_name,
                    description=description,
                    platform=platform,
                    difficulty=difficulty,
                    size=size,
                )
            )
            self._enforce_size(generated, size)

        generated["name"] = self._title_case(workflow_name)
        generated["description"] = description.strip()
        generated["platform"] = platform.strip()
        generated["difficulty"] = difficulty.strip()
        generated["size"] = size.strip()
        generated["generation_provider"] = provider_name
        generated["status"] = "Generated Draft"

        return generated

    @classmethod
    def _enforce_size(cls, workflow, requested_size):
        """Reject AI output that does not honor the requested node range."""
        normalized_size = requested_size.strip().title()
        if normalized_size not in cls.SIZE_LIMITS:
            raise ValueError("Choose a valid workflow size.")
        nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
        if not isinstance(nodes, dict):
            raise ValueError("The generated workflow does not contain valid nodes.")
        minimum, maximum = cls.SIZE_LIMITS[normalized_size]
        node_count = len(nodes)
        if not minimum <= node_count <= maximum:
            raise ValueError(
                f"The AI generated {node_count} nodes for a {normalized_size} workflow; "
                f"the required range is {minimum} to {maximum}. Please generate it again."
            )

    @staticmethod
    def _title_case(value):
        """Return a readable product title while preserving common acronyms."""
        small_words = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "with"}
        acronyms = {"ai": "AI", "dhcp": "DHCP", "dns": "DNS", "ip": "IP", "macos": "macOS", "vpn": "VPN", "wi-fi": "Wi-Fi"}
        words = value.strip().split()
        result = []
        for index, word in enumerate(words):
            key = word.lower()
            if key in acronyms:
                result.append(acronyms[key])
            elif 0 < index < len(words) - 1 and key in small_words:
                result.append(key)
            else:
                result.append(word[:1].upper() + word[1:].lower())
        return " ".join(result)
