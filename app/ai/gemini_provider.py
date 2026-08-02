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
        description=""
    ):
        prompt = f"""
You are creating content for SupportPilot.

Return ONLY valid JSON.

Do not wrap the JSON in markdown.

Command:
{command_name}

Additional context:
{description}

Return this exact schema:

{{
  "summary": "",
  "syntax": "",
  "examples": [],
  "important_fields": [],
  "common_errors": [],
  "related_commands": [],
  "related_articles": [],
  "official_references": []
}}
"""

        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        return json.loads(response.text)