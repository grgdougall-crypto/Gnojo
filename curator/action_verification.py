from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionVerificationProfile:
    """Code-owned identity for one reviewed post-action verification pattern."""

    workflow_id: str
    action_node_id: str
    action_family: str
    verification_key: str
    expected_current_destination: str
    required_destinations: tuple[str, ...]


VPN_SECURITY_CONFIGURATION_PROFILE = ActionVerificationProfile(
    workflow_id="vpn_connectivity_win",
    action_node_id="instr_configure_fw_av",
    action_family="approved_security_software_configuration",
    verification_key="vpn_approved_security_configuration_result",
    expected_current_destination="res_vpn_resolved",
    required_destinations=("res_vpn_resolved", "instr_check_adapter_status"),
)


ACTION_VERIFICATION_PROFILES = {
    (VPN_SECURITY_CONFIGURATION_PROFILE.workflow_id,
     VPN_SECURITY_CONFIGURATION_PROFILE.action_node_id): VPN_SECURITY_CONFIGURATION_PROFILE,
}


def action_verification_profile(
    workflow_id: str, action_node_id: str,
) -> ActionVerificationProfile | None:
    return ACTION_VERIFICATION_PROFILES.get((str(workflow_id or ""), str(action_node_id or "")))
