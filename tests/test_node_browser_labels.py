import unittest

from app.app import app


class NodeBrowserLabelTests(unittest.TestCase):
    def test_browser_uses_friendly_labels_and_keeps_id_as_metadata(self):
        response = app.test_client().get("/workflow-editor/vpn_connectivity_win.json")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<span class="workflow-node__meta">\n                    Instruction', html)
        self.assertIn('title="Instruction · Internal ID: instr_check_adapter_status"', html)
        self.assertNotIn('Instruction <span aria-hidden="true">·</span> instr_check_adapter_status', html)
        self.assertIn("Internal ID:", html)


if __name__ == "__main__":
    unittest.main()
