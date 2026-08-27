from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.curator_stage_b_reconciliation_service import (
    CuratorStageBReconciliationService,
    CuratorTerminalEvidenceCurrentEvidenceSyncService,
    CuratorTerminalEvidenceStageBReconciliationService,
)
from app.services.curator_structural_repair_approval_service import (
    CuratorStructuralRepairApprovalService,
)
from curator.__main__ import main
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryStore, LockedCuratorMemory
from curator.models import InventoryRecord
from curator.reconciliation import StageBJournalError


class CuratorStageBCurrentEvidenceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.write_workflow(self.workflow())
        state = self.store.load()
        state["controls"]["scheduled_runs_disabled"] = False
        state["tasks"] = {"GKT-TERMINAL": self.task_for_current_finding()}
        self.store.save(state)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(*, corrected: bool = False, alternate_predecessor: bool = False):
        gateway = "Ping the default gateway"
        if corrected:
            gateway += " and test external IP reachability"
        nodes = {
            "test_gateway": {
                "type": "instruction", "title": gateway,
                "instruction": gateway, "next": "dns_result",
            },
            "dns_result": {
                "type": "question",
                "question": "Did nslookup return an IP address?",
                "answers": {
                    "yes": {"label": "Yes", "next": "complete"},
                    "no": {
                        "label": "No",
                        "next": "confirm_dns" if alternate_predecessor else "dns_problem",
                    },
                },
            },
            "dns_problem": {"type": "resolution", "title": "DNS Resolution Problem"},
            "complete": {"type": "resolution", "title": "Complete"},
        }
        if alternate_predecessor:
            nodes["confirm_dns"] = {
                "type": "question", "question": "Did DNS resolution fail?",
                "answers": {"yes": {"label": "Yes", "next": "dns_problem"}},
            }
        return {
            "workflow_id": "network_diagnostics",
            "name": "Network Diagnostics",
            "start_node": "test_gateway",
            "nodes": nodes,
        }

    def write_workflow(self, workflow, filename="network_diagnostics.json"):
        directory = self.root / "app" / "workflow_drafts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(workflow), encoding="utf-8")

    def current_finding(self):
        workflow = json.loads(
            (self.root / "app/workflow_drafts/network_diagnostics.json").read_text(
                encoding="utf-8"
            )
        )
        record = InventoryRecord(
            "workflow", "network_diagnostics", "Network Diagnostics",
            "app/workflow_drafts/network_diagnostics.json", "Networking",
            "Windows", "draft", workflow,
        )
        return next(
            finding for finding in CuratorChecks(self.root).run_record(record)
            if finding.rule == "CUR-WR-TERMINAL-EVIDENCE"
            and finding.content_identifier == "network_diagnostics:dns_problem"
        )

    def task_for_current_finding(self, **updates):
        finding = self.current_finding()
        value = {
            "task_id": "GKT-TERMINAL",
            "finding_id": finding.identifier,
            "durable_identity": (
                "CUR-WR-TERMINAL-EVIDENCE|workflow_node|"
                "network_diagnostics:dns_problem|workflow_reasoning_evidence_gap"
            ),
            "status": "open", "owner": "Workflow Designer",
            "priority": "Medium", "classification": "Risk",
            "review_disposition": "NOT_REVIEWED",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "related_workflows": ["network_diagnostics"],
            "evidence": ["Original evidence snapshot"],
            "current_evidence": list(finding.evidence),
            "structured_evidence": copy.deepcopy(finding.structured_evidence),
            "times_observed": 3, "last_seen": "2026-08-28T00:00:00+00:00",
            "trend": "recurring", "knowledge_debt_score": 9,
            "history": [], "resolution_history": [],
            "resolution_package": {"status": "draft_created"},
        }
        value.update(updates)
        return value

    def verification_service(self):
        return CuratorTerminalEvidenceStageBReconciliationService(
            self.root, now=lambda: datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
        )

    def sync_service(self, **values):
        return CuratorTerminalEvidenceCurrentEvidenceSyncService(
            self.root,
            now=lambda: datetime(2026, 8, 28, 3, 5, tzinfo=timezone.utc),
            **values,
        )

    def make_verification_fresh(self):
        result = self.verification_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "COMMITTED")

    def make_evidence_stale(self):
        task = self.current_task()
        task["current_evidence"] = ["Stale evidence"]
        task["structured_evidence"] = {
            **copy.deepcopy(task["structured_evidence"]),
            "predecessor_edges": [{
                "source": "old_source", "route": "no",
                "destination": "dns_problem",
            }],
        }
        self.replace_task(task)

    def replace_task(self, task):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task}
        self.store.save(state)

    def current_task(self):
        return self.store.load()["tasks"]["GKT-TERMINAL"]

    def test_stale_current_and_structured_evidence_synchronize_exactly(self):
        self.make_verification_fresh()
        self.make_evidence_stale()
        expected = self.current_finding()
        before = copy.deepcopy(self.current_task())

        result = self.sync_service().run(task_id="GKT-TERMINAL")
        after = self.current_task()

        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_evidence"], list(expected.evidence))
        self.assertEqual(after["structured_evidence"], expected.structured_evidence)
        changed = {
            field for field in set(before) | set(after)
            if before.get(field) != after.get(field)
        }
        self.assertEqual(changed, {"current_evidence", "structured_evidence"})
        for field in (
            "evidence", "status", "owner", "priority", "classification",
            "last_seen", "times_observed", "trend", "knowledge_debt_score",
            "history", "resolution_package", "finding_id", "provenance",
        ):
            self.assertEqual(after.get(field), before.get(field), field)

    def test_upstream_predecessor_change_synchronizes_new_exact_edge(self):
        old_edge = self.current_task()["structured_evidence"]["predecessor_edges"]
        self.write_workflow(self.workflow(alternate_predecessor=True))
        self.make_verification_fresh()
        before_fingerprint = self.current_task()["last_verified_fingerprint"]
        result = self.sync_service().run(task_id="GKT-TERMINAL")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertNotEqual(
            after["structured_evidence"]["predecessor_edges"], old_edge
        )
        self.assertEqual(after["structured_evidence"]["predecessor_edges"], [{
            "source": "confirm_dns", "route": "Yes",
            "destination": "dns_problem",
        }])
        self.assertEqual(after["last_verified_fingerprint"], before_fingerprint)

    def test_already_synchronized_and_corrected_findings_skip_without_clearing(self):
        self.make_verification_fresh()
        synchronized_before = copy.deepcopy(self.current_task())
        synchronized = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(synchronized.task_results[0].status, "SKIPPED")
        self.assertEqual(self.current_task(), synchronized_before)

        self.write_workflow(self.workflow(corrected=True))
        self.make_verification_fresh()
        stale = self.current_task()
        stale["current_evidence"] = ["Preserve this evidence"]
        self.replace_task(stale)
        corrected_before = copy.deepcopy(self.current_task())
        corrected = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(corrected.task_results[0].status, "SKIPPED")
        self.assertEqual(self.current_task(), corrected_before)

    def test_missing_or_stale_capability_two_verification_skips(self):
        missing_before = copy.deepcopy(self.current_task())
        missing = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(missing.task_results[0].status, "SKIPPED")
        self.assertEqual(self.current_task(), missing_before)

        self.make_verification_fresh()
        self.write_workflow({**self.workflow(), "name": "Changed after verification"})
        stale_before = copy.deepcopy(self.current_task())
        stale = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(stale.task_results[0].status, "SKIPPED")
        self.assertIn("stale", stale.task_results[0].reason)
        self.assertEqual(self.current_task(), stale_before)

    def test_unsupported_and_nonactionable_tasks_skip_without_mutation(self):
        for updates in (
            {"curator_rule": "OTHER-RULE"},
            {"finding_type": "other_finding"},
            {"content_type": "workflow"},
            {"status": "resolved"},
            {"status": "deferred"},
            {"content_identifier": "network_diagnostics"},
            {"content_identifier": "network_diagnostics:dns_problem:extra"},
            {"related_workflows": ["other_workflow"]},
        ):
            with self.subTest(updates=updates):
                task = self.task_for_current_finding(**updates)
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.sync_service().run(task_id="GKT-TERMINAL")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertEqual(self.current_task(), before)

    def test_multiple_mismatch_and_incomplete_findings_fail_closed(self):
        self.make_verification_fresh()
        finding = self.current_finding()
        service = self.sync_service()
        with patch.object(service.checks, "run_record", return_value=[finding, finding]):
            multiple = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(multiple.task_results[0].status, "SKIPPED")

        task = self.current_task()
        task["finding_id"] = "CUR-MISMATCH"
        self.replace_task(task)
        mismatch = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(mismatch.task_results[0].status, "SKIPPED")

        fresh = self.task_for_current_finding()
        fresh["current_verification"] = copy.deepcopy(
            self.current_task()["current_verification"]
        )
        fresh["last_verified_fingerprint"] = self.current_task()[
            "last_verified_fingerprint"
        ]
        self.replace_task(fresh)
        incomplete = replace(finding, structured_evidence={"terminal": "dns_problem"})
        service = self.sync_service()
        with patch.object(service.checks, "run_record", return_value=[incomplete]):
            result = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("incomplete", result.task_results[0].reason)

    def test_pending_approval_and_ambiguous_application_state_skip(self):
        self.make_verification_fresh()
        approval_service = CuratorStructuralRepairApprovalService._for_test(
            self.root, task_loader=lambda _: copy.deepcopy(self.current_task()),
            now=lambda: datetime(2026, 8, 28, 3, 1, tzinfo=timezone.utc),
        )
        approval_service.issue(
            task_id="GKT-TERMINAL", workflow_filename="network_diagnostics.json",
            reviewer_identity="Reviewer", fix_session_id="CFX-EVIDENCE-SYNC",
        )
        self.make_evidence_stale()
        pending = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(pending.task_results[0].status, "SKIPPED")
        self.assertIn("pending", pending.task_results[0].reason.casefold())

        approval_root = self.root / "curation_memory/structural_repair_approvals"
        for path in approval_root.rglob("*"):
            if path.is_file():
                path.unlink()
        for path in sorted(approval_root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        approval_root.rmdir()
        ambiguous = self.root / "curation_memory/structural_repair_applications/bad"
        ambiguous.mkdir(parents=True)
        result = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("ambiguous", result.task_results[0].reason.casefold())

    def test_dry_run_is_complete_and_writes_nothing(self):
        self.make_verification_fresh()
        self.make_evidence_stale()
        before = self.files()
        result = self.sync_service().run(task_id="GKT-TERMINAL", dry_run=True)
        delta = result.task_results[0].proposed_delta
        self.assertEqual(result.task_results[0].status, "DRY_RUN")
        self.assertEqual(delta["capability"], {
            "id": "cur-wr-terminal-evidence-current-evidence-sync", "version": 1,
        })
        self.assertEqual(delta["confirmed_presence"], "still_detected")
        self.assertEqual(delta["verification_dependency"]["capability_version"], 1)
        self.assertEqual(
            set(delta["changed_fields"]),
            {"current_evidence", "structured_evidence"},
        )
        self.assertEqual(delta["eligibility"], "eligible")
        self.assertTrue(delta["unchanged"]["task_lifecycle"])
        self.assertTrue(delta["unchanged"]["ranking_and_debt"])
        self.assertTrue(delta["unchanged"]["trusted_content"])
        self.assertEqual(self.files(), before)

    def test_idempotency_and_crash_recovery_do_not_touch_task_history(self):
        self.make_verification_fresh()
        self.make_evidence_stale()
        history_before = copy.deepcopy(self.current_task()["history"])
        first = self.sync_service().run(task_id="GKT-TERMINAL")
        memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        second = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(second.task_results[0].status, "SKIPPED")
        self.assertEqual(first.task_results[0].idempotency_key,
                         second.task_results[0].idempotency_key)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(),
                         memory_before)
        events = self.sync_service().journal.get(first.task_results[0].idempotency_key)
        self.assertEqual(sum(item.status == "COMMITTED" for item in events), 1)
        self.assertEqual(self.current_task()["history"], history_before)

        self.write_workflow(self.workflow(alternate_predecessor=True))
        self.make_verification_fresh()
        self.make_evidence_stale()
        service = self.sync_service()
        append = service.journal.append

        def crash_after(event):
            if event.status == "COMMITTED":
                raise StageBJournalError("simulated journal interruption")
            return append(event)

        service.journal.append = crash_after
        failed = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(failed.task_results[0].status, "FAILED")
        recovered = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(recovered.task_results[0].status, "COMMITTED")
        self.assertIn("Recovered", recovered.task_results[0].reason)

    def test_no_external_artifact_or_repair_path_is_mutated(self):
        self.make_verification_fresh()
        self.make_evidence_stale()
        protected = []
        for relative in (
            "app/workflow_drafts/network_diagnostics.json",
            "app/workflow_publications/current.json",
            "knowledge_base/published/article.json",
            "curation_memory/resolution_packages/GKT-OTHER.json",
            "curation_memory/structural_recoveries/SRX-OTHER.json",
            "curation_memory/fix_sessions/CFX-OTHER.json",
        ):
            path = self.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sentinel", encoding="utf-8")
            protected.append(path)
        before = {str(path): path.read_bytes() for path in protected}
        with patch(
            "app.services.curator_targeted_verification_service."
            "CuratorTargetedVerificationService.verify",
            side_effect=AssertionError("mutating verification must not run"),
        ):
            result = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual({str(path): path.read_bytes() for path in protected}, before)
        self.assertFalse(
            (self.root / "curation_memory/structural_repair_applications").exists()
        )
        self.assertFalse(
            (self.root / "curation_memory/structural_repair_approvals").exists()
        )

    def test_shared_controls_lock_cas_and_cli_dispatch_remain_bounded(self):
        self.make_verification_fresh()
        self.make_evidence_stale()
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        disabled = self.sync_service().run(task_id="GKT-TERMINAL")
        self.assertEqual(disabled.task_results[0].status, "FAILED")
        state = self.store.load()
        state["controls"]["global_disabled"] = False
        self.store.save(state)
        with self.store.locked():
            locked = self.sync_service(lock_timeout=0.01).run(task_id="GKT-TERMINAL")
        self.assertEqual(locked.task_results[0].status, "FAILED")

        def change_owner(plan, attempt):
            if attempt == 0:
                current = self.store.load()
                current["tasks"]["GKT-TERMINAL"]["owner"] = "Human"
                self.store.save(current)

        service = self.sync_service()
        service._before_commit = change_owner
        committed = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(committed.task_results[0].status, "COMMITTED")
        self.assertEqual(self.current_task()["owner"], "Human")

        self.make_evidence_stale()
        before = self.files()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "sync-terminal-evidence", "--repository", str(self.root),
                "--task-id", "GKT-TERMINAL", "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn(
            '"capability_id": "cur-wr-terminal-evidence-current-evidence-sync"',
            output.getvalue(),
        )
        self.assertEqual(self.files(), before)

    def test_existing_capability_types_and_generic_scope_are_unchanged(self):
        self.assertEqual(
            CuratorStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-progress-verification-refresh",
        )
        self.assertEqual(
            CuratorTerminalEvidenceStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-terminal-evidence-verification-refresh",
        )
        self.assertEqual(
            CuratorStageBReconciliationService.MUTATION_FIELDS,
            ("current_verification", "last_verified_fingerprint", "history"),
        )
        self.assertEqual(
            CuratorTerminalEvidenceCurrentEvidenceSyncService.MUTATION_FIELDS,
            ("current_evidence", "structured_evidence"),
        )

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
