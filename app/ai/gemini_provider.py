from pathlib import Path
import json
import os

from dotenv import load_dotenv
from google import genai

from app.ai.provider import AIProvider

load_dotenv()


class GeminiProvider(AIProvider):
    """
    Generates SupportPilot content using Google's Gemini API.
    """

    def __init__(self):
        self.client = genai.Client(
            api_key=os.getenv("GEMINI_API_KEY")
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

        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "prompts"
            / "command_prompt.txt"
        )

        prompt = prompt_path.read_text(
            encoding="utf-8"
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

        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        response_text = (
            response.text or ""
        ).strip()

        if not response_text:
            raise RuntimeError(
                "Gemini returned an empty response."
            )

        try:
            generated = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Gemini returned invalid JSON."
            ) from error

        if not isinstance(generated, dict):
            raise RuntimeError(
                "Gemini returned an unexpected response structure."
            )

        return generated