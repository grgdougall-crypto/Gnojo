from __future__ import annotations

import re
from typing import Any


class CuratorRelationshipRepairProposalService:
    """Build a conservative, read-only proposal for one reciprocity conflict."""

    SUPPORTED_FINDING = "article_command_reciprocity_conflict"

    _FACETS = {
        "adapter_link": (
            "adapter link", "link status", "link speed", "network cable", "ethernet cable",
            "wired ethernet", "physical connection", "link lights", "cable is unplugged",
        ),
        "ip_configuration": (
            "ip configuration", "ip address", "subnet mask", "default gateway", "dns server",
            "tcp/ip configuration", "dhcp information",
        ),
        "ip_reachability": (
            "icmp", "echo request", "remote host", "host can be reached", "packet loss",
            "response time", "round-trip", "ping traffic",
        ),
        "storage_capacity": (
            "remaining space", "free space", "storage capacity", "low storage", "disk space",
            "cleanup recommendation", "temporary files", "application caches", "safe cleanup",
        ),
        "filesystem_integrity": (
            "file-system error", "filesystem error", "file system error", "ntfs scan",
            "logical file-system", "logical filesystem", "volume repair", "chkdsk",
        ),
        "process_resource": (
            "running process", "windows processes", "processes using", "process resource",
            "processor time", "cpu time", "memory usage", "working set", "task manager",
        ),
    }
    _INCOMPATIBLE = {
        frozenset(("filesystem_integrity", "storage_capacity")),
        frozenset(("ip_reachability", "adapter_link")),
    }

    def build(self, task: dict[str, Any], relationship: dict[str, Any] | None) -> dict[str, Any] | None:
        if task.get("finding_type") != self.SUPPORTED_FINDING:
            return None
        if not relationship or not relationship.get("target_found"):
            return self._withhold(relationship, "The authoritative command record is unavailable.")

        command = relationship.get("command_context") or {}
        articles = relationship.get("articles") or []
        if len(articles) != 1:
            return self._withhold(
                relationship,
                "The task implicates more than one article, so one unambiguous metadata mutation cannot be proposed.",
            )
        article = articles[0]
        if not article.get("found"):
            return self._withhold(relationship, "The implicated published article is unavailable.")

        command_id = str(command.get("id") or relationship.get("affected_id") or "")
        article_id = str(article.get("id") or "")
        command_declares = article_id in (relationship.get("related_articles") or [])
        article_declares = command_id in (article.get("related_commands") or [])
        common = self._base(relationship, article, command_declares, article_declares)
        if not command_id or not article_id:
            return {**common, **self._human("Canonical command or article identity is missing.")}
        if command_declares == article_declares:
            return {**common, **self._human(
                "The current declarations are already aligned; Curator will not invent another mutation.")}

        command_text = self._text(command.get("title"), command.get("name"), command.get("summary"))
        article_text = self._text(
            article.get("title"), article.get("overview"), article.get("category"),
            *(article.get("tags") or []),
        )
        command_facets = self._facets(command_text)
        article_facets = self._facets(article_text)
        shared = sorted(command_facets & article_facets)
        conflict = any(pair <= (command_facets | article_facets)
                       and pair & command_facets and pair & article_facets
                       for pair in self._INCOMPATIBLE)
        referenced = self._structured_reference(command, article)

        if shared or (referenced and self._meaningful_overlap(command_text, article_text)):
            if command_declares:
                change = f"Add '{command_id}' to related_commands."
                path = str(article.get("source_path") or "")
                field = "related_commands"
            else:
                change = f"Add '{article_id}' to related_articles."
                path = str(relationship.get("source_path") or "")
                field = "related_articles"
            rationale = (
                "The current purposes share specific diagnostic evidence"
                + (f" ({', '.join(name.replace('_', ' ') for name in shared)})." if shared else ".")
                + " Reciprocity can therefore be proposed as a consistency repair."
            )
            return {**common, "outcome": "add_reciprocal", "rationale": rationale,
                    "metadata_change": change, "affected_record": path,
                    "affected_field": field, "change_applied": False,
                    "status_message": "Proposal only — no metadata change has been applied."}

        if conflict:
            if command_declares:
                change = f"Remove '{article_id}' from related_articles."
                path = str(relationship.get("source_path") or "")
                field = "related_articles"
            else:
                change = f"Remove '{command_id}' from related_commands."
                path = str(article.get("source_path") or "")
                field = "related_commands"
            return {**common, "outcome": "remove_unsupported",
                    "rationale": "The records describe distinct diagnostic layers or purposes; broad topical adjacency is not enough to justify the relationship.",
                    "metadata_change": change, "affected_record": path,
                    "affected_field": field, "change_applied": False,
                    "status_message": "Proposal only — no metadata change has been applied."}

        return {**common, **self._human(
            "The available purpose metadata does not establish either specific semantic support or a known purpose conflict.")}

    def _base(self, relationship: dict[str, Any], article: dict[str, Any],
              command_declares: bool, article_declares: bool) -> dict[str, Any]:
        command = relationship.get("command_context") or {}
        return {
            "command_id": str(command.get("id") or relationship.get("affected_id") or ""),
            "command_title": str(command.get("title") or command.get("name") or ""),
            "command_purpose": str(command.get("summary") or ""),
            "article_id": str(article.get("id") or ""),
            "article_title": str(article.get("title") or ""),
            "article_purpose": str(article.get("overview") or ""),
            "command_declares_article": command_declares,
            "article_declares_command": article_declares,
        }

    def _withhold(self, relationship: dict[str, Any] | None, reason: str) -> dict[str, Any]:
        relationship = relationship or {}
        articles = relationship.get("articles") or []
        article = articles[0] if len(articles) == 1 else {}
        return {**self._base(relationship, article, False, False), **self._human(reason)}

    @staticmethod
    def _human(reason: str) -> dict[str, Any]:
        return {"outcome": "human_review_required", "rationale": reason,
                "metadata_change": "No metadata mutation proposed.", "affected_record": "",
                "affected_field": "", "change_applied": False,
                "status_message": "Curator is intentionally withholding a repair recommendation."}

    @classmethod
    def _facets(cls, text: str) -> set[str]:
        return {name for name, phrases in cls._FACETS.items() if any(phrase in text for phrase in phrases)}

    @staticmethod
    def _text(*values: Any) -> str:
        return " ".join(str(value or "").casefold().replace("-", " ") for value in values)

    @staticmethod
    def _structured_reference(command: dict[str, Any], article: dict[str, Any]) -> bool:
        identifiers = {
            str(command.get("id") or "").casefold(),
            str(command.get("name") or "").casefold(),
        } - {""}
        for value in article.get("structured_commands") or []:
            first = re.split(r"\s|\|", str(value).strip().casefold(), maxsplit=1)[0]
            if first in identifiers:
                return True
        return False

    @staticmethod
    def _meaningful_overlap(left: str, right: str) -> bool:
        stop = {"the", "and", "for", "with", "from", "this", "that", "windows", "command",
                "guidance", "information", "using", "without", "current", "system"}
        words = lambda value: {word for word in re.findall(r"[a-z0-9]+", value)
                               if len(word) > 3 and word not in stop}
        return len(words(left) & words(right)) >= 2
