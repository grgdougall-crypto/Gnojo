from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .memory import CuratorMemoryStore
from .shadow_rules import proportional_safety_hierarchy_v1


RUNTIME_RULE_SCHEMA = "1.0"
KNOWN_VARIANTS = {"proportional_safety_hierarchy_v1"}


class CuratorRuntimeRuleError(RuntimeError):
    pass


def validate_runtime_rule(value: dict[str, Any], *, expected_rule_id: str = "") -> dict[str, Any]:
    """Validate and normalize a constrained, declarative production rule manifest."""
    manifest = deepcopy(value or {})
    required = {"schema_version", "rule_id", "variant", "parameters"}
    missing = sorted(required - set(manifest))
    if missing:
        raise CuratorRuntimeRuleError(f"Runtime rule is missing: {', '.join(missing)}")
    if manifest["schema_version"] != RUNTIME_RULE_SCHEMA:
        raise CuratorRuntimeRuleError("Unsupported runtime rule schema.")
    if expected_rule_id and manifest["rule_id"] != expected_rule_id:
        raise CuratorRuntimeRuleError("Runtime rule identifier does not match the proposal.")
    if manifest["variant"] not in KNOWN_VARIANTS:
        raise CuratorRuntimeRuleError("Runtime rule variant is not registered.")
    if manifest["rule_id"] != "CUR-SAFE-L1" or manifest["variant"] != "proportional_safety_hierarchy_v1":
        raise CuratorRuntimeRuleError("This rule and variant combination is not supported.")
    if manifest["parameters"] != {"accept_stronger_levels": True}:
        raise CuratorRuntimeRuleError("Runtime rule parameters are outside the registered contract.")
    return manifest


def runtime_rule_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ActiveRuleRegistry:
    """Loads only human-activated, shadow-tested, immutable registered variants."""

    def __init__(self, rules: dict[str, dict[str, Any]] | None = None):
        self.rules = deepcopy(rules or {})

    @classmethod
    def from_repository(cls, repository_root: Path) -> "ActiveRuleRegistry":
        state = CuratorMemoryStore(repository_root.resolve() / "curation_memory").load()
        rules: dict[str, dict[str, Any]] = {}
        for proposal in state["growth"]["proposals"].values():
            if proposal.get("kind") != "audit_rule" or proposal.get("status") != "active":
                continue
            manifest = proposal.get("activated_runtime_rule")
            activation = proposal.get("activation") or {}
            history = proposal.get("decision_history") or []
            approved = any(item.get("to") == "human_approved" for item in history)
            activated = next((item for item in reversed(history) if item.get("to") == "active"), None)
            shadow = (proposal.get("shadow_results") or [{}])[-1]
            if not (manifest and approved and activated and shadow.get("passed")):
                continue
            try:
                manifest = validate_runtime_rule(manifest, expected_rule_id=proposal.get("rule_id", ""))
            except CuratorRuntimeRuleError:
                continue
            fingerprint = runtime_rule_fingerprint(manifest)
            if activation.get("manifest_fingerprint") != fingerprint:
                continue
            rules[manifest["rule_id"]] = {
                "manifest": manifest,
                "proposal_id": proposal["proposal_id"],
                "activated_by": activation.get("actor"),
                "activated_at": activation.get("at"),
                "manifest_fingerprint": fingerprint,
            }
        return cls(rules)

    def get(self, rule_id: str) -> dict[str, Any] | None:
        return deepcopy(self.rules.get(rule_id))

    def has_proportional_safety(self, node: dict[str, Any], level: int) -> bool | None:
        active = self.rules.get(f"CUR-SAFE-L{level}")
        if not active:
            return None
        variant = active["manifest"]["variant"]
        if variant == "proportional_safety_hierarchy_v1":
            return proportional_safety_hierarchy_v1(node, level)
        return None
