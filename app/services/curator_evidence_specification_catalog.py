from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable

from app.services.curator_structural_repair_contracts import EvidenceProbeSpecification


class EvidenceSpecificationCatalogError(ValueError):
    """A code-owned production evidence specification catalog is invalid."""


class CuratorEvidenceSpecificationCatalog:
    """Immutable, source-controlled approved evidence probe catalog."""

    def __init__(self, specifications: Iterable[EvidenceProbeSpecification | dict[str, Any]]):
        values = []
        identities = set()
        evidence_versions = set()
        for raw in specifications:
            try:
                specification = (
                    raw if isinstance(raw, EvidenceProbeSpecification)
                    else EvidenceProbeSpecification.from_dict(raw)
                )
            except (TypeError, ValueError) as error:
                raise EvidenceSpecificationCatalogError(str(error)) from error
            if not specification.approved:
                raise EvidenceSpecificationCatalogError(
                    "Production evidence specifications must be explicitly approved."
                )
            identity = (specification.specification_id, specification.version)
            if identity in identities:
                raise EvidenceSpecificationCatalogError(
                    f"Duplicate evidence specification identity/version: {identity[0]} v{identity[1]}."
                )
            evidence_version = (specification.evidence_key, specification.version)
            if evidence_version in evidence_versions:
                raise EvidenceSpecificationCatalogError(
                    f"Multiple specifications declare evidence key '{specification.evidence_key}' "
                    f"at version {specification.version}."
                )
            identities.add(identity)
            evidence_versions.add(evidence_version)
            values.append(specification)
        self._specifications = tuple(sorted(values, key=lambda item: (
            item.evidence_key, item.version, item.specification_id
        )))
        latest = {}
        for item in self._specifications:
            latest[item.evidence_key] = item
        self._by_evidence_key = MappingProxyType(latest)

    def lookup(self, evidence_key: str, version: int | None = None) -> EvidenceProbeSpecification | None:
        key = str(evidence_key or "")
        if version is None:
            return self._by_evidence_key.get(key)
        return next((item for item in self._specifications
                     if item.evidence_key == key and item.version == version), None)

    def all(self) -> tuple[EvidenceProbeSpecification, ...]:
        return self._specifications


EXTERNAL_IP_REACHABILITY_SPECIFICATION = {
    "specification_id": "external-ip-reachability-windows-v1",
    "version": 1,
    "evidence_key": "external_ip_reachability",
    "approved": True,
    "approved_by": "Gnojo technical review",
    "approved_at": "2026-08-24T00:00:00+00:00",
    "evidence_node": {
        "node_id": "test_external_ip_reachability",
        "content": {
            "type": "instruction",
            "title": "Test External IP Reachability",
            "instruction": (
                "Use an organization-approved external IP address supplied by your support team. "
                "In Command Prompt, run ping -n 4 followed by that approved IP address and record "
                "whether replies are received. Do not change DNS or network settings."
            ),
            "help_text": (
                "This read-only test checks whether the host can receive replies from an external IP "
                "address without relying on DNS name resolution. A reply establishes external IP "
                "reachability for this test. No reply may also reflect filtering or blocked ICMP, so "
                "do not treat it alone as proof that internet access is unavailable. If no approved "
                "target is available, record that the evidence could not be established."
            ),
            "next": "external_ip_reachability_result",
        },
    },
    "result_node": {
        "node_id": "external_ip_reachability_result",
        "content": {
            "type": "question",
            "question": "Did the organization-approved external IP address return ping replies?",
            "help_text": (
                "Choose Replies received only when the approved external IP returned replies. "
                "Choose External reachability not established when there were no replies, ICMP may "
                "be blocked, or no approved target was available."
            ),
            "answers": {
                "replies_received": {
                    "label": "Replies received",
                    "next": "$preserved_terminal",
                },
                "not_established": {
                    "label": "External reachability not established",
                    "next": "$reviewed_external_reachability_failure_destination",
                },
            },
        },
    },
    "result_routes": {
        "replies_received": "$preserved_terminal",
        "not_established": "$reviewed_external_reachability_failure_destination",
    },
}


EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2 = {
    "specification_id": "external-ip-reachability-windows-v2",
    "version": 2,
    "evidence_key": "external_ip_reachability",
    "approved": True,
    "approved_by": "Gnojo technical review",
    "approved_at": "2026-08-24T00:00:00+00:00",
    "evidence_node": EXTERNAL_IP_REACHABILITY_SPECIFICATION["evidence_node"],
    "result_node": {
        "node_id": "external_ip_reachability_result",
        "content": {
            "type": "question",
            "question": "Did the organization-approved external IP address return ping replies?",
            "help_text": (
                "Choose Replies received only when the approved external IP returned replies. "
                "Choose External reachability not established when there were no replies, ICMP may "
                "be blocked, or no approved target was available."
            ),
            "answers": {
                "replies_received": {
                    "label": "Replies received",
                    "next": "$preserved_terminal",
                },
                "not_established": {
                    "label": "External reachability not established",
                    "next": "external_connectivity_unclear",
                },
            },
        },
    },
    "result_routes": {
        "replies_received": "$preserved_terminal",
        "not_established": "external_connectivity_unclear",
    },
    "outcome_nodes": [{
        "node_id": "external_connectivity_unclear",
        "terminal_semantics": "bounded_diagnostic_uncertainty",
        "required_evidence": [
            "classified_local_ipv4_configuration",
            "gateway_reachability",
            "dns_resolution_test_failed",
            "external_ip_reachability_not_established",
        ],
        "content": {
            "type": "resolution",
            "title": "External Connectivity Could Not Be Confirmed",
            "message": (
                "The default gateway responded, but DNS lookup failed and the approved external-IP "
                "test did not establish reachability. This does not distinguish a DNS problem from "
                "upstream connectivity loss or filtered ICMP traffic. Record the configured DNS "
                "servers, approved test address, command results, exact time, and any firewall or "
                "security messages. Have the appropriate network or security administrator review "
                "the evidence before changing DNS, firewall, proxy, or managed network settings."
            ),
        },
    }],
}


PRODUCTION_EVIDENCE_SPECIFICATIONS = CuratorEvidenceSpecificationCatalog((
    EXTERNAL_IP_REACHABILITY_SPECIFICATION,
    EXTERNAL_IP_REACHABILITY_SPECIFICATION_V2,
))
