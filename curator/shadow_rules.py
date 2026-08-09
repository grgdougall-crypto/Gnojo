from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


SAFETY_EVIDENCE = {
    1: ("wait", "close", "reopen", "brief interruption"),
    2: ("save", "active work", "disconnect", "approval"),
    3: ("backup", "restore point", "rollback", "recovery"),
    4: ("administrator", "administrative", "backup", "approval", "power"),
}

SAFETY_PATTERNS = {
    1: (
        r"\bclose\b.{0,40}\breopen\b",
        r"\bbrief interruption\b",
        r"\bwait\b",
        r"\bsave (?:any |your |active )?(?:work|documents?|files?)\b.{0,80}\b(?:restart\w*|reopen\w*|relaunch\w*|clos\w*)\b",
        r"\b(?:lose|lost) unsaved work\b",
    ),
    2: (r"\bsave\b.{0,30}\bactive work\b", r"\bdisconnect\b", r"\bapproval\b"),
    3: (r"\bbackup\b", r"\brestore point\b", r"\brollback\b", r"\brecovery\b"),
    4: (r"\badministrat(?:or|ive)\b", r"\bbackup\b", r"\bapproval\b", r"\bpower\b"),
}


def proportional_safety_hierarchy_v1(node: dict[str, Any], level: int) -> bool:
    """Registered deterministic predicate: stronger precautions satisfy a lower level."""
    guidance = " ".join(str(node.get(key) or "") for key in
                        ("warning", "prerequisites", "rollback", "help_text", "instruction")).casefold()
    accepted = tuple(pattern for candidate_level, patterns in SAFETY_PATTERNS.items()
                     if candidate_level >= level for pattern in patterns)
    return any(re.search(pattern, guidance) for pattern in accepted)


# Backward-compatible name retained for the shadow harness and stored evidence.
proposed_proportional_safety = proportional_safety_hierarchy_v1


def run_level_one_shadow(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate an explicit fixture set without registering or activating a rule."""
    matrix = {key: 0 for key in ("true_positive", "true_negative", "false_positive", "false_negative")}
    uncertain = 0
    results = []
    for fixture in fixtures:
        level = int(fixture.get("level") or 1)
        actual_finding = not proposed_proportional_safety(fixture["node"], level)
        expected_finding = fixture.get("expected_finding")
        if expected_finding is None:
            outcome = "uncertain"
            uncertain += 1
        elif actual_finding and expected_finding:
            outcome = "true_positive"
        elif not actual_finding and not expected_finding:
            outcome = "true_negative"
        elif actual_finding:
            outcome = "false_positive"
        else:
            outcome = "false_negative"
        if outcome != "uncertain":
            matrix[outcome] += 1
        results.append({"name": fixture["name"], "expected_finding": expected_finding,
                        "actual_finding": actual_finding, "outcome": outcome, "level": level})
    return {
        "passed": matrix["false_positive"] == 0 and matrix["false_negative"] == 0,
        "confusion_matrix": matrix,
        "fixture_results": deepcopy(results),
        "uncertain": uncertain,
        "findings": matrix["true_positive"] + matrix["false_positive"],
        "false_positives": matrix["false_positive"],
    }
