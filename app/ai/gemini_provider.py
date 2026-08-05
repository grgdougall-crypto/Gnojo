from pathlib import Path
import json
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.ai.provider import AIProvider

load_dotenv(override=True)


class GeminiProvider(AIProvider):
    """
    Generates structured Gnojo content
    using Google's Gemini API.
    """

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured."
            )

        self.client = genai.Client(
            api_key=api_key
        )

    def generate_command(
        self,
        command_name,
        description="",
    ):
        """
        Generate one structured command draft.
        """

        normalized_name = command_name.strip()
        normalized_description = description.strip()

        prompt = self._load_prompt(
            "command_prompt.txt"
        )

        prompt = (
            prompt
            .replace(
                "COMMAND_NAME",
                normalized_name,
            )
            .replace(
                "DESCRIPTION",
                normalized_description
                or "No additional context was provided.",
            )
        )

        return self._generate_json(
            prompt=prompt,
            content_type="command",
        )

    def generate_workflow(
        self,
        workflow_name,
        description="",
        platform="Windows",
        difficulty="Beginner",
        size="Medium",
    ):
        """
        Generate one structured troubleshooting
        workflow draft.
        """

        normalized_name = workflow_name.strip()
        normalized_description = description.strip()
        normalized_platform = platform.strip()
        normalized_difficulty = difficulty.strip()
        normalized_size = size.strip()

        prompt = self._load_prompt(
            "workflow_prompt.txt"
        )

        prompt = (
            prompt
            .replace(
                "WORKFLOW_NAME",
                normalized_name,
            )
            .replace(
                "DESCRIPTION",
                normalized_description
                or "No additional context was provided.",
            )
            .replace(
                "PLATFORM",
                normalized_platform,
            )
            .replace(
                "DIFFICULTY",
                normalized_difficulty,
            )
            .replace(
                "WORKFLOW_SIZE",
                normalized_size,
            )
        )

        return self._generate_json(
            prompt=prompt,
            content_type="workflow",
        )

    def generate_workflow_node_suggestion(self, prompt):
        """Return one structured workflow-node writing suggestion."""
        return self._generate_json(
            prompt=prompt,
            content_type="workflow node suggestion",
        )

    def find_authoritative_sources(self, prompt):
        """Find current web sources using Google Search grounding."""
        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        response_text = self._remove_code_fence((response.text or "").strip())
        if not response_text:
            raise RuntimeError("Gemini returned no grounded source suggestions.")
        return json.loads(response_text)

    def _load_prompt(
        self,
        prompt_filename,
    ):
        """
        Load a prompt file from app/prompts.
        """

        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "prompts"
            / prompt_filename
        )

        if not prompt_path.exists():
            raise RuntimeError(
                f"Prompt file '{prompt_filename}' was not found."
            )

        return prompt_path.read_text(
            encoding="utf-8"
        )

    def _generate_json(
        self,
        prompt,
        content_type,
    ):
        """
        Send a prompt to Gemini and return
        a validated JSON dictionary.
        """

        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        response_text = (
            response.text or ""
        ).strip()

        if not response_text:
            raise RuntimeError(
                f"Gemini returned an empty {content_type} response."
            )

        response_text = self._remove_code_fence(
            response_text
        )

        try:
            generated = json.loads(
                response_text
            )
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Gemini returned invalid {content_type} JSON."
            ) from error

        if not isinstance(generated, dict):
            raise RuntimeError(
                f"Gemini returned an unexpected "
                f"{content_type} response structure."
            )

        return generated

    def _remove_code_fence(
        self,
        response_text,
    ):
        """
        Remove Markdown JSON fences when a provider
        includes them around the response.
        """

        cleaned_text = response_text.strip()

        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        elif cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]

        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]

        return cleaned_text.strip()
