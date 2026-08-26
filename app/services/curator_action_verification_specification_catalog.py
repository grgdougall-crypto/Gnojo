from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable

from app.services.curator_structural_repair_contracts import (
    ActionVerificationSpecification,
)


class ActionVerificationSpecificationCatalogError(ValueError):
    """A code-owned post-action verification catalog is invalid."""


class CuratorActionVerificationSpecificationCatalog:
    """Immutable reviewed specifications; catalog membership grants no apply authority."""

    def __init__(
        self,
        specifications: Iterable[ActionVerificationSpecification | dict[str, Any]],
    ):
        values = []
        identities = set()
        keys = set()
        for raw in specifications:
            try:
                specification = (
                    raw if isinstance(raw, ActionVerificationSpecification)
                    else ActionVerificationSpecification.from_dict(raw)
                )
            except (TypeError, ValueError) as error:
                raise ActionVerificationSpecificationCatalogError(str(error)) from error
            if not specification.approved:
                raise ActionVerificationSpecificationCatalogError(
                    "Production action-verification specifications must be approved."
                )
            identity = (specification.specification_id, specification.version)
            key = (specification.verification_key, specification.version)
            if identity in identities or key in keys:
                raise ActionVerificationSpecificationCatalogError(
                    "Action-verification specification identity/version must be unique."
                )
            identities.add(identity)
            keys.add(key)
            values.append(specification)
        self._specifications = tuple(sorted(
            values,
            key=lambda item: (item.verification_key, item.version, item.specification_id),
        ))
        latest = {}
        for item in self._specifications:
            latest[item.verification_key] = item
        self._by_key = MappingProxyType(latest)

    def lookup(
        self, verification_key: str, version: int | None = None,
    ) -> ActionVerificationSpecification | None:
        key = str(verification_key or "")
        if version is None:
            return self._by_key.get(key)
        return next((item for item in self._specifications
                     if item.verification_key == key and item.version == version), None)

    def all(self) -> tuple[ActionVerificationSpecification, ...]:
        return self._specifications


VPN_APPROVED_SECURITY_CONFIGURATION_VERIFICATION = {
    "specification_id": "vpn-approved-security-configuration-verification-v1",
    "version": 1,
    "verification_key": "vpn_approved_security_configuration_result",
    "action_family": "approved_security_software_configuration",
    "workflow_id": "vpn_connectivity_win",
    "action_node_id": "instr_configure_fw_av",
    "expected_current_destination": "res_vpn_resolved",
    "approved": True,
    "approved_by": "Gnojo technical review",
    "approved_at": "2026-08-25T00:00:00+00:00",
    "verification_node": {
        "node_id": "q_configured_fw_av_works",
        "content": {
            "type": "question",
            "title": "VPN After Approved Security Configuration",
            "question": (
                "After applying the approved security-software configuration and retrying "
                "the connection, does the VPN connect successfully?"
            ),
            "help_text": (
                "Use the result of a new VPN connection attempt. If you could not complete or "
                "verify the approved configuration, choose the uncertainty option and continue "
                "without making broader security changes."
            ),
            "answers": {
                "yes": {
                    "label": "Yes, the VPN connects",
                    "next": "res_vpn_resolved",
                },
                "no": {
                    "label": "No, it still does not connect",
                    "next": "instr_check_adapter_status",
                },
                "unsure": {
                    "label": "I'm not sure",
                    "next": "instr_check_adapter_status",
                },
            },
        },
    },
    "result_routes": {
        "yes": "res_vpn_resolved",
        "no": "instr_check_adapter_status",
        "unsure": "instr_check_adapter_status",
    },
    "platform_constraints": ["Windows"],
    "safety_constraints": [
        "Keep firewall and security protections enabled.",
        "Only an approved configuration change may be tested.",
        "Uncertainty continues diagnostics without broader security changes.",
    ],
    "forbidden_mutations": [
        "action_content",
        "unrelated_routes",
        "publication",
        "task_state",
        "security_configuration",
    ],
}


PRODUCTION_ACTION_VERIFICATION_SPECIFICATIONS = (
    CuratorActionVerificationSpecificationCatalog((
        VPN_APPROVED_SECURITY_CONFIGURATION_VERIFICATION,
    ))
)
