import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.engine.content_generation_engine import ContentGenerationEngine
from app.engine.workflow_generation_engine import WorkflowGenerationEngine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("gemini_key", "openai_key"),
    [
        (None, None),
        (None, "test-openai-key"),
        ("test-gemini-key", None),
        ("test-gemini-key", "test-openai-key"),
    ],
)
def test_flask_app_boots_for_each_provider_configuration(gemini_key, openai_key):
    environment = os.environ.copy()
    environment.pop("GEMINI_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    if gemini_key is not None:
        environment["GEMINI_API_KEY"] = gemini_key
    if openai_key is not None:
        environment["OPENAI_API_KEY"] = openai_key

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from unittest.mock import patch\n"
                "with patch('dotenv.load_dotenv', return_value=False):\n"
                "    from app.app import app\n"
                "    assert app is not None\n"
                "    assert app.test_client().get('/').status_code == 200\n"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "engine_class",
    [ContentGenerationEngine, WorkflowGenerationEngine],
)
def test_provider_clients_are_not_constructed_with_generation_engine(engine_class):
    module = sys.modules[engine_class.__module__]

    with (
        patch.object(module, "GeminiProvider") as gemini_provider,
        patch.object(module, "OpenAIProvider") as openai_provider,
    ):
        engine_class()

    gemini_provider.assert_not_called()
    openai_provider.assert_not_called()


def test_command_generation_uses_existing_openai_fallback_when_gemini_unavailable():
    fallback = Mock()
    fallback.generate_command.return_value = {
        "description": "Display network configuration.",
    }

    with (
        patch(
            "app.engine.content_generation_engine.GeminiProvider",
            side_effect=RuntimeError("GEMINI_API_KEY is not configured."),
        ),
        patch(
            "app.engine.content_generation_engine.OpenAIProvider",
            return_value=fallback,
        ),
    ):
        generated = ContentGenerationEngine().generate_command("ipconfig")

    assert generated["generation_provider"] == "OpenAI"
    fallback.generate_command.assert_called_once_with("ipconfig", "")


def test_command_generation_uses_gemini_without_constructing_openai():
    primary = Mock()
    primary.generate_command.return_value = {
        "description": "Display network configuration.",
    }

    with (
        patch(
            "app.engine.content_generation_engine.GeminiProvider",
            return_value=primary,
        ),
        patch(
            "app.engine.content_generation_engine.OpenAIProvider"
        ) as openai_provider,
    ):
        generated = ContentGenerationEngine().generate_command("ipconfig")

    assert generated["generation_provider"] == "Gemini"
    openai_provider.assert_not_called()


def test_workflow_generation_uses_existing_openai_fallback_when_gemini_unavailable():
    fallback = Mock()
    fallback.generate_workflow.return_value = {
        "nodes": {f"step_{index}": {} for index in range(14)},
    }

    with (
        patch(
            "app.engine.workflow_generation_engine.GeminiProvider",
            side_effect=RuntimeError("GEMINI_API_KEY is not configured."),
        ),
        patch(
            "app.engine.workflow_generation_engine.OpenAIProvider",
            return_value=fallback,
        ),
    ):
        generated = WorkflowGenerationEngine().generate_workflow(
            "network diagnostics"
        )

    assert generated["generation_provider"] == "OpenAI"
    fallback.generate_workflow.assert_called_once_with(
        workflow_name="network diagnostics",
        description="",
        platform="Windows",
        difficulty="Beginner",
        size="Medium",
    )


def test_unavailable_providers_fail_only_when_generation_is_invoked(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = ContentGenerationEngine()

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not configured"):
        engine.generate_command("ipconfig")
