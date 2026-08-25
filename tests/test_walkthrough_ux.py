import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


class WalkthroughUxCorrectionTests(unittest.TestCase):
    def test_home_cta_describes_its_existing_workflow_picker_destination(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="#workflows"', source)
        self.assertIn("Choose a Workflow", source)
        self.assertNotIn("Start Troubleshooting", source)

    def test_terminal_feedback_prompt_covers_diagnosis_and_resolution(self):
        source = (TEMPLATES / "wizard.html").read_text(encoding="utf-8")
        self.assertIn("Did this help identify or resolve the problem?", source)
        self.assertNotIn("Did this solve the problem?", source)
        for value in ('value="yes"', 'value="partially"', 'value="no"'):
            self.assertIn(value, source)


if __name__ == "__main__":
    unittest.main()
