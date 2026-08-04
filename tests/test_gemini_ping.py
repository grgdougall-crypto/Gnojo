import os
import unittest
from unittest.mock import patch

from app.knowledge.providers.gemini_provider import GeminiProvider


class GeminiProviderConfigurationTests(unittest.TestCase):
    def test_provider_reports_configuration_without_network_access(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"}):
            provider = GeminiProvider()

        self.assertTrue(provider.is_configured())
        self.assertEqual(provider.provider_name, "gemini")
        self.assertEqual(provider.model_name, "test-model")


if __name__ == "__main__":
    unittest.main()
