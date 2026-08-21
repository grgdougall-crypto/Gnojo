from dataclasses import dataclass
from typing import Optional


@dataclass
class Node:
    """
    Represents a single node within a troubleshooting
    decision tree.
    """

    id: str
    type: str

    question: Optional[str] = None
    title: Optional[str] = None
    instruction: Optional[str] = None
    message: Optional[str] = None
    help_text: Optional[str] = None

    answers: Optional[dict] = None
    next: Optional[str] = None

    knowledge_article: Optional[str] = None

    # New: allows one workflow to transition into another
    next_workflow: Optional[str] = None
    button_label: Optional[str] = None
