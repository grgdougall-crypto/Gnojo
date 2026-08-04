from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SearchResult:
    """
    Represents a normalized result returned by SearchService,
    regardless of the underlying Gnojo content type.
    """

    id: str
    title: str
    summary: str
    content_type: str
    endpoint: str

    category: Optional[str] = None
    difficulty: Optional[str] = None
    icon: Optional[str] = None

    score: int = 0
    source: Optional[dict[str, Any]] = None