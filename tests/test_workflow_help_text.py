import unittest

from app.services.workflow_help_text_service import (
    WorkflowHelpTextError,
    WorkflowHelpTextService,
)


def help_workflow():
    return {
        "name": "Advanced Network Diagnostics",
        "description": "Inspect network configuration and connectivity.",
        "category": "Networking",
        "platform": "Windows",
        "difficulty": "Intermediate",
        "start_node": "inspect_ip",
        "nodes": {
            "inspect_ip": {
                "type": "instruction",
                "title": "Inspect the IP Configuration",
                "instruction": "Run ipconfig /all and record the active adapter's IPv4 address, default gateway, DNS servers, and DHCP status.",
                "next": "address_type",
            },
            "address_type": {
                "type": "question",
                "question": "What kind of IPv4 address does the active adapter have?",
                "answers": {
                    "configured": {"label": "A configured address", "next": "done"},
                },
            },
            "done": {"type": "resolution", "title": "Core Network Diagnostics Passed", "message": "The core checks passed."},
        },
    }


class FakeProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.prompt = None

    def generate_workflow_node_suggestion(self, prompt):
        self.prompt = prompt
        if self.error:
            raise self.error
        return self.response


class WorkflowHelpTextServiceTests(unittest.TestCase):
    def test_provider_receives_workflow_and_neighbor_context(self):
        provider = FakeProvider({
            "help_text": (
                "Record the active adapter's IPv4 address, default gateway, DNS servers, and DHCP status from ipconfig /all. "
                "A missing gateway or an automatic private address identifies a configuration clue, but this display-only result does not prove internet reachability."
            )
        })
        workflow = help_workflow()
        result = WorkflowHelpTextService(providers=[("Gemini", provider)]).suggest(
            workflow, "inspect_ip", workflow["nodes"]["inspect_ip"]
        )

        self.assertEqual(result["provider"], "Gemini")
        self.assertFalse(result["used_fallback"])
        self.assertIn("Advanced Network Diagnostics", provider.prompt)
        self.assertIn("address_type", provider.prompt)

    def test_invalid_first_provider_falls_through_to_second(self):
        generic = FakeProvider({"help_text": "This step checks whether the issue is fixed. Complete only the described action and note what changes."})
        specific = FakeProvider({
            "help_text": (
                "Record the IPv4 address, default gateway, DNS servers, and DHCP status shown by ipconfig /all. "
                "These values reveal configuration clues without changing network settings, but they do not confirm external connectivity."
            )
        })
        workflow = help_workflow()
        result = WorkflowHelpTextService(
            providers=[("Gemini", generic), ("OpenAI", specific)]
        ).suggest(workflow, "inspect_ip", workflow["nodes"]["inspect_ip"])

        self.assertEqual(result["provider"], "OpenAI")

    def test_provider_failures_use_contextual_local_fallback(self):
        workflow = help_workflow()
        result = WorkflowHelpTextService(providers=[
            ("Gemini", FakeProvider(error=RuntimeError("offline"))),
            ("OpenAI", FakeProvider(error=RuntimeError("offline"))),
        ]).suggest(workflow, "inspect_ip", workflow["nodes"]["inspect_ip"])

        self.assertTrue(result["used_fallback"])
        self.assertEqual(result["provider"], "Local fallback")
        self.assertIn("IPv4 address", result["help_text"])

    def test_gateway_rejection_recovers_with_safe_local_fallback(self):
        workflow = help_workflow()
        workflow["nodes"]["gateway"] = {
            "type": "instruction",
            "title": "Test the Default Gateway",
            "instruction": "Run ping followed by the default gateway address shown in ipconfig /all.",
            "next": "done",
        }
        contaminated = FakeProvider({
            "help_text": (
                "Ping the gateway, then record DNS servers, DHCP status, and IPv4 configuration from ipconfig /all. "
                "These details provide evidence without changing network settings."
            )
        })
        result = WorkflowHelpTextService(providers=[("Gemini", contaminated)]).suggest(
            workflow, "gateway", workflow["nodes"]["gateway"]
        )

        self.assertTrue(result["used_fallback"])
        self.assertIn("packet loss", result["help_text"])
        self.assertNotIn("DNS servers", result["help_text"])
        self.assertNotIn("DHCP", result["help_text"])

    def test_validation_rejects_generic_or_invented_guidance(self):
        service = WorkflowHelpTextService(providers=[])
        node = help_workflow()["nodes"]["inspect_ip"]
        with self.assertRaises(WorkflowHelpTextError):
            service.validate_candidate(node, "This step checks whether the selected network setting is correct. Complete only the described action and note what changes.")
        with self.assertRaises(WorkflowHelpTextError):
            service.validate_candidate(node, "Run tracert to inspect the IP configuration and record the route. This result provides network evidence without changing settings or proving that every destination is reachable.")

    def test_existing_help_text_and_neighbor_evidence_do_not_leak(self):
        workflow = help_workflow()
        workflow["nodes"]["inspect_ip"]["help_text"] = "Old generic guidance that should never enter the prompt."
        provider = FakeProvider({
            "help_text": (
                "Record the active adapter's IPv4 address, default gateway, DNS servers, and DHCP status from ipconfig /all. "
                "These values describe the selected configuration without changing it or proving external connectivity."
            )
        })
        WorkflowHelpTextService(providers=[("Gemini", provider)]).suggest(
            workflow, "inspect_ip", workflow["nodes"]["inspect_ip"]
        )
        self.assertNotIn("Old generic guidance", provider.prompt)

        gateway = {
            "type": "instruction",
            "title": "Test the Default Gateway",
            "instruction": "Run ping followed by the default gateway address shown in ipconfig /all.",
        }
        with self.assertRaises(WorkflowHelpTextError):
            WorkflowHelpTextService().validate_candidate(
                gateway,
                "Ping the default gateway and record whether it replies. Also record DNS servers, DHCP status, and IPv4 configuration even though those fields belong to another diagnostic step.",
            )


if __name__ == "__main__":
    unittest.main()
