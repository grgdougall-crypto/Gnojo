from __future__ import annotations

import re
from collections.abc import Iterable


def intentional_action_signals(
    values: Iterable[object], action_terms: Iterable[str]
) -> list[str]:
    """Find exact action terms used as directives, not incidental substrings."""
    signals: set[str] = set()
    non_action_followers = {
        "is", "are", "was", "were", "status", "pending", "scheduled",
        "complete", "completed", "required", "requirement",
    }
    for raw in values:
        text = str(raw or "").casefold()
        for term in action_terms:
            signal = str(term or "").casefold().strip()
            if not signal:
                continue
            for match in re.finditer(
                rf"(?<!\w){re.escape(signal)}(?!\w)", text
            ):
                prefix = text[:match.start()].rstrip()
                suffix = text[match.end():].lstrip()
                following = re.match(r"[a-z]+", suffix)
                if following and following.group(0) in non_action_followers:
                    continue
                imperative = not prefix or bool(re.search(
                    r"(?:^|[.!?;:,])\s*(?:then\s+|please\s+)?$", prefix
                ))
                directed = bool(re.search(
                    r"(?:\bto|\bplease|\bthen|\byou\s+(?:must|should|can|may|"
                    r"need\s+to|needs\s+to))\s*$",
                    prefix,
                ))
                if imperative or directed:
                    signals.add(signal)
    return sorted(signals)
