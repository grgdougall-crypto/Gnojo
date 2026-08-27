from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.curator_stage_b_reconciliation_service import (
    CuratorTerminalEvidenceStageBReconciliationService,
)
from app.services.curator_targeted_verification_service import (
    CuratorTargetedVerificationService,
)
from curator.__main__ import main
from curator.checks import CuratorChecks
from curator.memory import CuratorMemoryStore, LockedCuratorMemory
from curator.models import InventoryRecord
from curator.reconciliation import StageBJournalError
from curator.resolution import ResolutionPackageRepository


class CuratorStageBTerminalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.write_workflow(self.workflow())
        self.replace_task(self.task_for_current_finding())

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(*, corrected: bool = False, terminal_type: str = "resolution"):
        gateway_text = "Ping the default gateway"
        if corrected:
            gateway_text += " and test external IP reachability"
        return {
            "workflow_id": "network_diagnostics",
            "name": "Network Diagnostics",
            "start_node": "test_gateway",
            "nodes": {
                "test_gateway": {
                    "type": "instruction",
                    "title": gateway_text,
                    "instruction": gateway_text,
                    "next": "dns_result",
                },
                "dns_result": {
                    "type": "question",
                    "question": "Did nslookup return an IP address?",
                    "answers": {
                        "yes": {"label": "Yes", "next": "complete"},
                        "no": {"label": "No", "next": "dns_problem"},
                    },
                },
                "dns_problem": {
                    "type": terminal_type,
                    "title": "DNS Resolution Problem",
                },
                "complete": {"type": "resolution", "title": "Complete"},
            },
        }

    def service(self, **values):
        return CuratorTerminalEvidenceStageBReconciliationService(
            self.root,
            now=lambda: datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc),
            **values,
        )

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
            "status": "open",
            "owner": "Workflow Designer",
            "priority": "Medium",
            "classification": "Risk",
            "review_disposition": "NOT_REVIEWED",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "related_workflows": ["network_diagnostics"],
            "current_evidence": list(finding.evidence),
            "structured_evidence": copy.deepcopy(finding.structured_evidence),
            "times_observed": 3,
            "last_seen": "2026-08-27T00:00:00+00:00",
            "trend": "recurring",
            "knowledge_debt_score": 9,
            "history": [],
            "resolution_history": [],
        }
        value.update(updates)
        return value

    def replace_task(self, task):
        state = self.store.load()
        state["controls"]["scheduled_runs_disabled"] = False
        state["tasks"] = {task["task_id"]: task}
        self.store.save(state)

    def test_existing_defect_refreshes_still_detected_with_whole_workflow_scope(self):
        result = self.service().run(task_id="GKT-TERMINAL")
        task = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(task["current_verification"]["status"], "still_detected")
        self.assertEqual(
            task["current_verification"]["affected_fingerprint_scope"],
            "whole_workflow",
        )
        workflow = self.workflow()
        self.assertEqual(
            task["last_verified_fingerprint"],
            CuratorTargetedVerificationService.fingerprint(workflow),
        )

    def test_corrected_upstream_evidence_refreshes_appears_corrected(self):
        self.write_workflow(self.workflow(corrected=True))
        before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-TERMINAL")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "appears_corrected")
        self.assert_preserved(before, after)

    def test_upstream_only_change_changes_fingerprint_and_adds_one_event(self):
        first = self.service().run(task_id="GKT-TERMINAL")
        terminal_before = copy.deepcopy(self.workflow()["nodes"]["dns_problem"])
        self.write_workflow(self.workflow(corrected=True))
        second = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(
            self.workflow(corrected=True)["nodes"]["dns_problem"], terminal_before
        )
        self.assertNotEqual(
            first.task_results[0].idempotency_key,
            second.task_results[0].idempotency_key,
        )
        self.assertEqual(len(self.current_task()["history"]), 2)

    def test_missing_workflow_terminal_and_ambiguous_drafts_skip(self):
        path = self.root / "app/workflow_drafts/network_diagnostics.json"
        path.unlink()
        missing_workflow = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(missing_workflow.task_results[0].status, "SKIPPED")
        self.write_workflow(self.workflow())
        task = self.current_task()
        workflow = self.workflow()
        workflow["nodes"].pop("dns_problem")
        self.write_workflow(workflow)
        missing_terminal = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(missing_terminal.task_results[0].status, "SKIPPED")
        self.write_workflow(self.workflow(), "one.json")
        self.write_workflow(self.workflow(), "two.json")
        ambiguous = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(ambiguous.task_results[0].status, "SKIPPED")
        self.assertNotIn("current_verification", task)

    def test_non_resolution_terminal_skips(self):
        self.write_workflow(self.workflow(terminal_type="instruction"))
        result = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "SKIPPED")
        self.assertIn("no longer a terminal", result.task_results[0].reason)

    def test_wrong_identity_and_non_actionable_tasks_skip(self):
        cases = (
            {"curator_rule": "OTHER-RULE"},
            {"finding_type": "other_finding"},
            {"content_type": "workflow"},
            {"status": "resolved"},
            {"status": "deferred"},
            {"content_identifier": "network_diagnostics"},
            {"content_identifier": "network_diagnostics:dns_problem:extra"},
            {"related_workflows": ["other_workflow"]},
            {"structured_evidence": {"terminal": "other_terminal"}},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                task = self.task_for_current_finding(**updates)
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.service().run(task_id="GKT-TERMINAL")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertEqual(self.current_task(), before)

    def test_multiple_matches_and_changed_finding_identity_fail_closed(self):
        finding = self.current_finding()
        service = self.service()
        with patch.object(service.checks, "run_record", return_value=[finding, finding]):
            multiple = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(multiple.task_results[0].status, "SKIPPED")
        task = self.current_task()
        task["finding_id"] = "CUR-STALE"
        self.replace_task(task)
        mismatch = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(mismatch.task_results[0].status, "SKIPPED")

    def test_same_key_does_not_rewrite_memory_or_duplicate_history(self):
        first = self.service().run(task_id="GKT-TERMINAL")
        memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        history_before = copy.deepcopy(self.current_task()["history"])
        second = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(
            first.task_results[0].idempotency_key,
            second.task_results[0].idempotency_key,
        )
        self.assertEqual(second.task_results[0].status, "SKIPPED")
        self.assertEqual(
            (self.root / "curation_memory/memory.json").read_bytes(), memory_before
        )
        self.assertEqual(self.current_task()["history"], history_before)

    def test_dry_run_reports_identity_delta_and_unchanged_authority(self):
        before = self.files()
        result = self.service().run(task_id="GKT-TERMINAL", dry_run=True)
        proposed = result.task_results[0].proposed_delta
        self.assertEqual(result.task_results[0].status, "DRY_RUN")
        self.assertEqual(proposed["identity"]["workflow_id"], "network_diagnostics")
        self.assertEqual(proposed["identity"]["terminal_node_id"], "dns_problem")
        self.assertEqual(proposed["verification_result"], "still_detected")
        self.assertEqual(proposed["affected_fingerprint_scope"], "whole_workflow")
        self.assertEqual(proposed["unchanged"], {
            "task_lifecycle": True,
            "trusted_content": True,
            "publication": True,
        })
        self.assertEqual(self.files(), before)

    def test_only_verification_fields_change_and_packages_are_not_completed(self):
        package_path = self.root / "curation_memory/resolution_packages/GKT-TERMINAL.json"
        ResolutionPackageRepository(self.root / "curation_memory").save({
            "task_id": "GKT-TERMINAL",
            "status": "draft_created",
            "recommendation": "CREATE_NEW_ARTICLE",
        })
        protected = [
            self.root / "app/workflow_drafts/network_diagnostics.json",
            package_path,
        ]
        for relative in (
            "app/workflow_publications/current.json",
            "curation_memory/structural_repair_approvals/sentinel.json",
            "curation_memory/structural_repair_applications/sentinel.json",
            "curation_memory/structural_repair_recoveries/sentinel.json",
            "curation_memory/fix_sessions/sentinel.json",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("sentinel", encoding="utf-8")
            protected.append(path)
        artifacts_before = {str(path): path.read_bytes() for path in protected}
        task_before = copy.deepcopy(self.current_task())
        with patch.object(
            CuratorTargetedVerificationService,
            "verify",
            side_effect=AssertionError("mutating verifier must not be called"),
        ):
            result = self.service().run(task_id="GKT-TERMINAL")
        task_after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assert_preserved(task_before, task_after)
        self.assertEqual(
            {str(path): path.read_bytes() for path in protected}, artifacts_before
        )
        self.assertEqual(
            json.loads(package_path.read_text(encoding="utf-8"))["status"],
            "draft_created",
        )

    def test_controls_lock_and_journal_remain_shared(self):
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        disabled = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(disabled.task_results[0].status, "FAILED")
        state = self.store.load()
        state["controls"]["global_disabled"] = False
        self.store.save(state)
        with self.store.locked():
            locked = self.service(lock_timeout=0.01).run(task_id="GKT-TERMINAL")
        self.assertEqual(locked.task_results[0].status, "FAILED")
        committed = self.service().run(task_id="GKT-TERMINAL")
        events = self.service().journal.get(committed.task_results[0].idempotency_key)
        self.assertEqual([event.status for event in events], ["PREPARED", "COMMITTED"])

    def test_cas_replan_preserves_concurrent_human_state(self):
        service = self.service()

        def change_owner(plan, attempt):
            if attempt == 0:
                state = self.store.load()
                state["tasks"]["GKT-TERMINAL"]["owner"] = "Human"
                self.store.save(state)

        service._before_commit = change_owner
        result = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(self.current_task()["owner"], "Human")
        self.assertEqual(len(self.current_task()["history"]), 1)

    def test_crash_before_and_after_commit_retry_without_duplicate_history(self):
        before = (self.root / "curation_memory/memory.json").read_bytes()
        with patch.object(
            LockedCuratorMemory,
            "compare_and_swap",
            side_effect=RuntimeError("simulated crash before commit"),
        ):
            failed_before = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(failed_before.task_results[0].status, "FAILED")
        self.assertEqual(
            (self.root / "curation_memory/memory.json").read_bytes(), before
        )
        service = self.service()
        append = service.journal.append

        def crash_after(event):
            if event.status == "COMMITTED":
                raise StageBJournalError("simulated journal interruption")
            return append(event)

        service.journal.append = crash_after
        failed_after = service.run(task_id="GKT-TERMINAL")
        self.assertEqual(failed_after.task_results[0].status, "FAILED")
        self.assertEqual(len(self.current_task()["history"]), 1)
        recovered = self.service().run(task_id="GKT-TERMINAL")
        self.assertEqual(recovered.task_results[0].status, "COMMITTED")
        self.assertIn("Recovered", recovered.task_results[0].reason)
        self.assertEqual(len(self.current_task()["history"]), 1)

    def test_explicit_cli_dispatches_terminal_evidence_capability(self):
        before = self.files()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "refresh-terminal-evidence-verification",
                "--repository", str(self.root),
                "--task-id", "GKT-TERMINAL",
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn(
            '"capability_id": "cur-wr-terminal-evidence-verification-refresh"',
            output.getvalue(),
        )
        self.assertEqual(self.files(), before)

    def current_task(self):
        return self.store.load()["tasks"]["GKT-TERMINAL"]

    def assert_preserved(self, before, after):
        for field in (
            "status", "owner", "priority", "classification",
            "review_disposition", "current_evidence", "structured_evidence",
            "times_observed", "last_seen", "trend", "knowledge_debt_score",
            "resolution_history", "finding_id", "task_id",
        ):
            self.assertEqual(after.get(field), before.get(field), field)

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
