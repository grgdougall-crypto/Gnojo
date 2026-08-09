from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final


GOVERNANCE_VERSION: Final = "1.0"


@dataclass(frozen=True)
class OperationProfile:
    name: str
    permissions: tuple[str, ...]


PERMISSIONS: Final = frozenset({
    "read_trusted_content", "write_audit_output", "create_knowledge_tasks",
    "create_drafts", "preview_repairs", "execute_approved_repairs", "run_tests",
    "access_external_sources", "modify_application_code", "publish_content", "deploy",
})

PROFILES: Final = MappingProxyType({
    "audit": OperationProfile("audit", (
        "read_trusted_content", "write_audit_output", "create_knowledge_tasks",
    )),
    "targeted_audit": OperationProfile("targeted_audit", (
        "read_trusted_content", "write_audit_output", "create_knowledge_tasks",
    )),
    "assisted_draft": OperationProfile("assisted_draft", (
        "read_trusted_content", "write_audit_output", "create_knowledge_tasks", "create_drafts",
    )),
    "repair_preview": OperationProfile("repair_preview", (
        "read_trusted_content", "write_audit_output", "preview_repairs",
    )),
    "approved_repair": OperationProfile("approved_repair", (
        "read_trusted_content", "write_audit_output", "preview_repairs",
        "execute_approved_repairs", "run_tests",
    )),
    "source_research": OperationProfile("source_research", (
        "read_trusted_content", "write_audit_output", "access_external_sources",
    )),
})

PROHIBITED_ACTIONS: Final = (
    "rewrite_governance", "self_activate_capability", "create_unrestricted_tool",
    "self_grant_permission", "execute_unapproved_script", "publish_generated_content",
    "modify_authentication", "modify_authorization", "alter_production_infrastructure",
    "push_code", "merge_code", "deploy_code", "conceal_action", "delete_history",
)

HUMAN_APPROVALS: Final = (
    "governance_change", "rule_activation", "adapter_enablement", "permission_grant",
    "content_publication", "taxonomy_change", "security_policy_change", "product_scope_change",
)


class CuratorGovernanceError(RuntimeError):
    pass


class CuratorGovernancePolicy:
    """Code-owned policy. Curator memory may record decisions but cannot alter this object."""

    @staticmethod
    def snapshot() -> dict:
        return {
            "version": GOVERNANCE_VERSION,
            "immutable": True,
            "principle": "The Curator may propose its own growth, but it may not approve its own growth.",
            "permitted_tools": sorted(PERMISSIONS),
            "prohibited_actions": list(PROHIBITED_ACTIONS),
            "required_human_approvals": list(HUMAN_APPROVALS),
            "publishing_boundary": "Curator may prepare pending-review drafts; only a human may publish.",
            "repair_boundary": "Only enabled, deterministic adapters may execute after required confirmation.",
            "scheduling_boundary": "Scheduled runs may audit, remember, task, draft, and brief; never publish, deploy, or activate growth.",
            "data_boundary": "Use only the least-privilege operation profile and never grant additional access.",
            "escalation_rule": "Uncertain, editorial, destructive, security, taxonomy, and scope decisions require a human.",
            "emergency_disable": "Global disable blocks all Curator operations; scheduled disable blocks unattended runs.",
            "profiles": {name: asdict(profile) for name, profile in PROFILES.items()},
        }

    @staticmethod
    def permissions_for(profile: str) -> frozenset[str]:
        value = PROFILES.get(profile)
        if not value:
            raise CuratorGovernanceError(f"Unknown Curator operation profile: {profile}")
        return frozenset(value.permissions)

    @classmethod
    def authorize(cls, profile: str, permission: str, controls: dict | None = None) -> None:
        controls = controls or {}
        if controls.get("global_disabled"):
            raise CuratorGovernanceError("Curator is globally disabled by a human operator.")
        if permission not in PERMISSIONS:
            raise CuratorGovernanceError(f"Unknown Curator permission: {permission}")
        if permission not in cls.permissions_for(profile):
            raise CuratorGovernanceError(
                f"Operation profile '{profile}' does not grant '{permission}'."
            )

