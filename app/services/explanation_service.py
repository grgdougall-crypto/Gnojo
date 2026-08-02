from app.models.command_explanation import CommandExplanation

class ExplanationService:
    """
    Builds structured command explanations from curated command data.
    """

    def explain_command(self, command, related_commands=None):
        """
        Return a structured explanation for one command.
        """

        related_commands = related_commands or []

        command_name = command.get(
            "name",
            "this command",
        )

        category = command.get(
            "category",
            "technical",
        )

        output_fields = command.get(
            "output_fields",
            [],
        )

        permissions = command.get(
            "permissions",
            {},
        )

        risk = command.get(
            "risk",
            {},
        )

        next_steps = [
            {
                "id": related_command.get("id", ""),
                "name": related_command.get("name", ""),
                "summary": related_command.get("summary", ""),
            }
            for related_command in related_commands
        ]

        return CommandExplanation(
            title=f"Understanding {command_name}",
            purpose=self._build_purpose(
                command,
            ),
            when_to_use=self._build_when_to_use(
                command_name,
                category,
            ),
            what_to_check=output_fields[:4],
            interpretation=self._build_interpretation(
                command,
            ),
            common_mistake=self._build_common_mistake(
                command,
            ),
            requires_elevation=permissions.get(
                "requires_elevation",
                False,
            ),
            permissions_notes=permissions.get(
                "notes",
                "",
            ),
            risk_level=risk.get(
                "level",
                "Unknown",
            ),
            risk_warning=risk.get(
                "warning",
                "No risk guidance is available.",
            ),
            next_steps=next_steps,
            narrative="",
        )

    def _build_purpose(self, command):
        """
        Build a concise explanation of why the command matters.
        """

        command_name = command.get(
            "name",
            "This command",
        )

        summary = command.get(
            "summary",
            "No summary is available.",
        )

        if command_name == "ipconfig":
            return (
                "ipconfig is usually one of the first commands to run "
                "when troubleshooting Windows network connectivity. "
                "It quickly shows whether the computer received a usable "
                "IP configuration and which gateway and DNS servers it "
                "is attempting to use."
            )

        if command_name == "ping":
            return (
                "ping is a quick connectivity test. It helps determine "
                "whether another device can be reached and whether replies "
                "are returning within a reasonable amount of time."
            )

        return summary

    def _build_when_to_use(self, command_name, category):
        """
        Build practical guidance about when the command is useful.
        """

        if command_name == "ipconfig":
            return (
                "Use ipconfig when internet access is unavailable, DHCP "
                "problems are suspected, DNS settings may be incorrect, "
                "or you need to verify the active adapter configuration."
            )

        if command_name == "ping":
            return (
                "Use ping after confirming the local network configuration. "
                "It can test the default gateway, another device, a public "
                "IP address, or a hostname."
            )

        return (
            f"Use {command_name} when troubleshooting "
            f"{category.lower()} issues and you need information that can "
            "help narrow down the cause."
        )

    def _build_interpretation(self, command):
        """
        Build practical interpretation guidance.
        """

        command_name = command.get(
            "name",
            "",
        )

        if command_name == "ipconfig":
            return [
                {
                    "title": "IPv4 begins with 169.254",
                    "message": (
                        "Windows could not obtain an address from DHCP. "
                        "Check the network connection, adapter status, "
                        "router, switch, or DHCP service."
                    ),
                },
                {
                    "title": "Default Gateway is blank",
                    "message": (
                        "The computer may communicate locally but cannot "
                        "reach other networks or the internet."
                    ),
                },
                {
                    "title": "DNS Servers are missing or unexpected",
                    "message": (
                        "The network may be reachable while website names "
                        "still fail to resolve."
                    ),
                },
            ]

        if command_name == "ping":
            return [
                {
                    "title": "Reply received",
                    "message": (
                        "The destination responded. Review latency and packet "
                        "loss to judge the quality of the connection."
                    ),
                },
                {
                    "title": "Request timed out",
                    "message": (
                        "No reply was received. The destination may be down, "
                        "unreachable, or configured to block ICMP."
                    ),
                },
                {
                    "title": "Host name fails but IP address works",
                    "message": (
                        "Basic connectivity is working, but DNS resolution "
                        "is likely the problem."
                    ),
                },
            ]

        return []

    def _build_common_mistake(self, command):
        """
        Build one beginner-focused warning.
        """

        command_name = command.get(
            "name",
            "",
        )

        if command_name == "ipconfig":
            return (
                "A common mistake is checking only whether Wi-Fi says "
                "'connected.' The adapter can appear connected while still "
                "having an invalid address, missing gateway, or incorrect "
                "DNS configuration."
            )

        if command_name == "ping":
            return (
                "A failed ping does not always mean the destination is down. "
                "Firewalls and network policies often block ICMP replies."
            )

        return ""