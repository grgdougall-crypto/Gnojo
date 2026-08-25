import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from app.repositories.structural_repair_application_repository import (
    StructuralRepairApplicationRepository,
    StructuralRepairApplicationRepositoryError,
)
from app.services.curator_evidence_specification_catalog import (
    PRODUCTION_EVIDENCE_SPECIFICATIONS,
)
from app.services.curator_repair_adapter_registry import CuratorRepairAdapterRegistry
from app.services.curator_structural_repair_governance import (
    STAGE3_SCHEMA_VERSION,
    STRUCTURAL_REPAIR_FAILURE_CATEGORIES,
    StructuralRepairApplicationRecord,
    StructuralRepairApproval,
    StructuralRepairFingerprint,
    StructuralRepairGovernanceError,
)


class StructuralRepairStage30ContractTests(unittest.TestCase):
    digest = "a" * 64

    @classmethod
    def approval_data(cls):
        return {
            "schema_version": STAGE3_SCHEMA_VERSION,
            "approval_id": "SRA-0123456789ABCDEF",
            "application_id": "SRX-0123456789ABCDEF",
            "task_id": "GKT-STRUCTURAL",
            "finding_id": "CUR-STRUCTURAL",
            "fix_session_id": "CFX-0123456789AB",
            "reviewer_identity": "Review Operator",
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": "network_diagnostics",
            "workflow_filename": "network_diagnostics.json",
            "workflow_lifecycle": "draft",
            "workflow_path": "app/workflow_drafts/network_diagnostics.json",
            "workflow_raw_sha256_before": cls.digest,
            "workflow_semantic_sha256_before": "b" * 64,
            "adapter_id": "missing_required_upstream_evidence",
            "plan_id": "SRP-0123456789AB",
            "plan_digest": "c" * 64,
            "specification_id": "external-ip-reachability-windows-v2",
            "specification_version": 2,
            "specification_digest": "d" * 64,
            "preview_digest": "e" * 64,
            "created_at": "2026-08-24T22:00:00+00:00",
            "expires_at": "2026-08-24T22:30:00+00:00",
            "approval_state": "approved",
        }

    @classmethod
    def record_data(cls, *, revision=1, event_id="SRE-0123456789ABCDEF",
                    previous="", outcome="pending", failure=""):
        approval = cls.approval_data()
        return {
            "schema_version": STAGE3_SCHEMA_VERSION,
            "application_id": approval["application_id"],
            "approval_id": approval["approval_id"],
            "event_id": event_id,
            "revision": revision,
            "previous_event_digest": previous,
            "task_id": approval["task_id"],
            "finding_id": approval["finding_id"],
            "fix_session_id": approval["fix_session_id"],
            "reviewer_identity": approval["reviewer_identity"],
            "reviewer_identity_assurance": "application_supplied",
            "workflow_id": approval["workflow_id"],
            "workflow_path": approval["workflow_path"],
            "workflow_raw_sha256_before": approval["workflow_raw_sha256_before"],
            "workflow_semantic_sha256_before": approval["workflow_semantic_sha256_before"],
            "expected_workflow_raw_sha256_after": "",
            "expected_workflow_semantic_sha256_after": "",
            "preview_digest": approval["preview_digest"],
            "plan_digest": approval["plan_digest"],
            "adapter_id": approval["adapter_id"],
            "specification_id": approval["specification_id"],
            "specification_version": approval["specification_version"],
            "specification_digest": approval["specification_digest"],
            "proposed_node_ids": ["test_external", "external_result", "external_unclear"],
            "changed_edges": [{"source": "dns_result", "route": "No", "destination": "dns_problem"}],
            "new_edges": [
                {"source": "test_external", "route": "next", "destination": "external_result"},
                {"source": "external_result", "route": "not_established", "destination": "external_unclear"},
            ],
            "created_at": approval["created_at"],
            "applied_at": "",
            "finalized_at": "",
            "validation_summaries": {"schema": {"passed": True}, "findings": []},
            "outcome": outcome,
            "failure_category": failure,
            "failure_reason": "",
            "rollback_status": "not_required",
            "rollback_raw_sha256": "",
            "rollback_semantic_sha256": "",
        }

    def test_approval_is_complete_immutable_and_stable(self):
        first = StructuralRepairApproval.from_dict(self.approval_data())
        second = StructuralRepairApproval.from_dict(copy.deepcopy(self.approval_data()))

        self.assertEqual(first, second)
        self.assertEqual(first.identity_digest, second.identity_digest)
        with self.assertRaises(FrozenInstanceError):
            first.approval_state = "changed"

    def test_approval_rejects_missing_fields_bad_time_or_non_draft_target(self):
        cases = []
        missing = self.approval_data()
        missing.pop("preview_digest")
        cases.append(missing)
        naive = self.approval_data()
        naive["created_at"] = "2026-08-24T22:00:00"
        cases.append(naive)
        expired = self.approval_data()
        expired["expires_at"] = expired["created_at"]
        cases.append(expired)
        published = self.approval_data()
        published["workflow_lifecycle"] = "published"
        cases.append(published)
        traversal = self.approval_data()
        traversal["workflow_path"] = "../workflow.json"
        cases.append(traversal)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(StructuralRepairGovernanceError):
                    StructuralRepairApproval.from_dict(value)

    def test_application_record_is_validated_and_nested_content_is_immutable(self):
        record = StructuralRepairApplicationRecord.from_dict(self.record_data())

        self.assertEqual(record.outcome, "pending")
        self.assertTrue(record.validation_summaries["schema"]["passed"])
        with self.assertRaises(TypeError):
            record.validation_summaries["schema"]["passed"] = False
        with self.assertRaises(AttributeError):
            record.validation_summaries["findings"].append("changed")

    def test_failure_categories_are_bounded(self):
        required = {
            "approval_missing", "approval_invalid", "approval_expired", "preview_unknown",
            "plan_invalid", "stale_workflow", "lock_unavailable", "validation_failed_prewrite",
            "persistence_failed", "validation_failed_postwrite", "rollback_succeeded",
            "rollback_failed", "already_applied",
        }
        self.assertEqual(STRUCTURAL_REPAIR_FAILURE_CATEGORIES, required)
        invalid = self.record_data(outcome="failed", failure="arbitrary_failure")
        missing = self.record_data(outcome="failed", failure="")
        for value in (invalid, missing):
            with self.assertRaises(StructuralRepairGovernanceError):
                StructuralRepairApplicationRecord.from_dict(value)

    def test_raw_and_semantic_fingerprints_have_distinct_contracts(self):
        compact = b'{"workflow_id":"flow","nodes":{"a":1}}'
        formatted = b'{\n  "nodes": {"a": 1},\n  "workflow_id": "flow"\n}\n'
        first = json.loads(compact)
        second = json.loads(formatted)

        self.assertNotEqual(StructuralRepairFingerprint.raw_workflow(compact),
                            StructuralRepairFingerprint.raw_workflow(formatted))
        self.assertEqual(StructuralRepairFingerprint.semantic_workflow(first),
                         StructuralRepairFingerprint.semantic_workflow(second))
        self.assertEqual(StructuralRepairFingerprint.raw_workflow(compact),
                         "c6b160b5d8dfcc4da9778f468b18ea97b9d88adee8062b35a43b182898bfe52a")

    def test_canonical_digest_preserves_list_order_and_rejects_unsupported_data(self):
        self.assertNotEqual(
            StructuralRepairFingerprint.contract({"routes": ["a", "b"]}),
            StructuralRepairFingerprint.contract({"routes": ["b", "a"]}),
        )
        for value in ({"bad": {"set"}}, {"bad": float("nan")}, [b"bytes"]):
            with self.subTest(value=value):
                with self.assertRaises(StructuralRepairGovernanceError):
                    StructuralRepairFingerprint.contract(value)

    def test_plan_preview_and_immutable_specification_digest_deterministically(self):
        specification = PRODUCTION_EVIDENCE_SPECIFICATIONS.lookup("external_ip_reachability")
        preview = {"plan": {"plan_id": "SRP-1", "routes": ["yes", "no"]}, "available": True}

        first_spec = StructuralRepairFingerprint.contract(specification)
        second_spec = StructuralRepairFingerprint.contract(specification)
        first_preview = StructuralRepairFingerprint.contract(preview)

        self.assertEqual(first_spec, second_spec)
        self.assertEqual(first_preview, StructuralRepairFingerprint.contract(copy.deepcopy(preview)))
        self.assertEqual(specification.version, 2)

    def test_history_is_append_only_chained_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StructuralRepairApplicationRepository(Path(directory) / "curation_memory")
            first = StructuralRepairApplicationRecord.from_dict(self.record_data())
            repository.append(first)
            second_data = self.record_data(
                revision=2, event_id="SRE-FEDCBA9876543210", previous=first.event_digest,
                outcome="failed", failure="validation_failed_prewrite",
            )
            repository.append(second_data)

            history = repository.get(first.application_id)
            self.assertEqual([item.revision for item in history], [1, 2])
            self.assertEqual(repository.list_application_ids(), (first.application_id,))
            with self.assertRaises(FrozenInstanceError):
                history[0].outcome = "changed"
            self.assertEqual(repository.get(first.application_id)[0].outcome, "pending")

    def test_history_rejects_duplicate_overwrite_and_malformed_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StructuralRepairApplicationRepository(Path(directory) / "curation_memory")
            first = StructuralRepairApplicationRecord.from_dict(self.record_data())
            repository.append(first)
            with self.assertRaises(StructuralRepairApplicationRepositoryError):
                repository.append(first)

            path = (Path(directory) / "curation_memory" / "structural_repair_applications"
                    / first.application_id / "000002-SRE-FEDCBA9876543210.json")
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(StructuralRepairApplicationRepositoryError):
                repository.get(first.application_id)

    def test_stage30_has_no_workflow_or_execution_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / "app" / "workflow_drafts" / "flow.json"
            workflow.parent.mkdir(parents=True)
            workflow.write_bytes(b'{"workflow_id":"flow"}')
            before = workflow.read_bytes()
            repository = StructuralRepairApplicationRepository(root / "curation_memory")
            repository.append(self.record_data())

            self.assertEqual(workflow.read_bytes(), before)
            self.assertFalse(CuratorRepairAdapterRegistry().lookup(
                "CUR-WR-TERMINAL-EVIDENCE", "workflow_reasoning_evidence_gap"
            ).executable)
            self.assertFalse(hasattr(repository, "apply"))
            self.assertFalse(hasattr(repository, "write_workflow"))


if __name__ == "__main__":
    unittest.main()
