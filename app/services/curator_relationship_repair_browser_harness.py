from __future__ import annotations

import json
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

from curator.memory import CuratorMemoryStore


class CuratorRelationshipRepairBrowserHarness:
    """Process-local Phase 3 browser fixtures; never points at the application repository."""

    CASES = (
        ("GKT-HARNESS-ADD", "adapter-tool", "ethernet-link-check",
         "Shows adapter link status and link speed.",
         "Check whether a wired Ethernet cable has a physical connection and link lights."),
        ("GKT-HARNESS-REMOVE", "reachability-tool", "physical-link-check",
         "Tests a remote host using ICMP echo requests and response time.",
         "Verify a physical Ethernet connection using cable and link lights."),
        ("GKT-HARNESS-HUMAN", "generic-tool", "generic-guide",
         "Shows diagnostic details.", "General troubleshooting guidance."),
    )

    def __init__(self):
        self._lock = Lock()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.root = Path()
        self.reset()

    def reset(self) -> Path:
        with self._lock:
            if self._temporary is not None:
                self._temporary.cleanup()
            self._temporary = tempfile.TemporaryDirectory(prefix="gnojo-phase3-browser-")
            self.root = Path(self._temporary.name).resolve()
            self._seed()
            return self.root

    def _seed(self) -> None:
        tasks: dict[str, dict[str, Any]] = {}
        for task_id, command_id, article_id, summary, overview in self.CASES:
            self._write(f"knowledge_base/commands/{command_id}.json", {
                "id": command_id, "title": command_id.replace("-", " ").title(),
                "name": command_id, "summary": summary, "category": "Diagnostics",
                "platforms": ["Windows 11"], "tags": [],
                "related_articles": [article_id], "related_commands": [],
                "fixture_marker": "Temporary Phase 3 browser fixture",
            })
            self._write(f"knowledge_base/published/{article_id}.json", {
                "id": article_id, "canonical_id": article_id,
                "title": article_id.replace("-", " ").title(), "overview": overview,
                "category": "Diagnostics", "tags": [], "related_commands": [], "commands": [],
                "fixture_marker": "Temporary Phase 3 browser fixture",
            })
            tasks[task_id] = {
                "task_id": task_id, "title": "Temporary relationship repair fixture",
                "status": "open", "owner": "Curator", "priority": "Medium",
                "classification": "Defect", "confidence": "high", "knowledge_debt_score": 1,
                "curator_rule": "CUR-REL-ARTICLE-COMMAND-RECIPROCITY-001",
                "finding_type": "article_command_reciprocity_conflict",
                "content_type": "command", "content_identifier": command_id,
                "evidence": [f"Article: {article_id}", f"Command: {command_id}"],
                "history": [{"event": "fixture_created", "note": "Temporary development/test data."}],
            }
        CuratorMemoryStore(self.root / "curation_memory").save({"tasks": tasks})

    def _write(self, relative: str, value: dict[str, Any]) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


_harness: CuratorRelationshipRepairBrowserHarness | None = None
_harness_lock = Lock()


def phase3_browser_harness() -> CuratorRelationshipRepairBrowserHarness:
    global _harness
    with _harness_lock:
        if _harness is None:
            _harness = CuratorRelationshipRepairBrowserHarness()
        return _harness
