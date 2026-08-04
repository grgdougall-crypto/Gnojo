from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommandExplanation:
    """
    Structured Gnojo explanation for a command.
    """

    title: str
    purpose: str
    when_to_use: str

    what_to_check: list[dict[str, Any]] = field(
        default_factory=list
    )

    interpretation: list[dict[str, str]] = field(
        default_factory=list
    )

    common_mistake: str = ""

    requires_elevation: bool = False
    permissions_notes: str = ""

    risk_level: str = "Unknown"
    risk_warning: str = ""

    next_steps: list[dict[str, str]] = field(
        default_factory=list
    )

    narrative: str = ""