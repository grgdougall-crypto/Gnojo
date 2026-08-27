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
    CuratorEarlyConvergenceStageBReconciliationService,
    CuratorStageBReconciliationService,
    CuratorTerminalEvidenceCurrentEvidenceSyncService,
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


class CuratorStageBEarlyConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.write_workflow(self.workflow())
        state = self.store.load()
        state["controls"]["scheduled_runs_disabled"] = False
        state["tasks"] = {"GKT-EARLY": self.task_for_current_finding()}
        self.store.save(state)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def workflow(*, corrected: bool = False, origin_type: str = "question"):
        if corrected:
            cpu_next, disk_next = "cpu_done", "disk_done"
        else:
            cpu_next = disk_next = "shared"
        return {
            "workflow_id": "early_convergence",
            "name": "Early Convergence Fixture",
            "category": "Diagnostics",
            "platform": "Windows",
            "start_node": "resource_question",
            "nodes": {
                "resource_question": {
                    "type": origin_type,
                    "question": "Which resource is constrained?",
                    "instruction": "Choose the constrained resource.",
                    "answers": {
                        "cpu": {"label": "CPU", "next": "cpu_check"},
                        "disk": {"label": "Disk", "next": "disk_check"},
                    },
                },
                "cpu_check": {
                    "type": "instruction", "title": "Inspect CPU",
                    "instruction": "Inspect CPU usage.", "next": cpu_next,
                },
                "disk_check": {
                    "type": "instruction", "title": "Inspect Disk",
                    "instruction": "Inspect disk usage.", "next": disk_next,
                },
                "shared": {
                    "type": "resolution", "title": "Additional Diagnostics Required",
                },
                "cpu_done": {"type": "resolution", "title": "CPU Pressure"},
                "disk_done": {"type": "resolution", "title": "Disk Pressure"},
            },
        }

    def write_workflow(self, workflow, filename="early_convergence.json"):
        directory = self.root / "app" / "workflow_drafts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(workflow), encoding="utf-8")

    def current_finding(self):
        workflow = json.loads(
            (self.root / "app/workflow_drafts/early_convergence.json").read_text(
                encoding="utf-8"
            )
        )
        record = InventoryRecord(
            "workflow", "early_convergence", "Early Convergence Fixture",
            "app/workflow_drafts/early_convergence.json", "Diagnostics",
            "Windows", "draft", workflow,
        )
        return next(
            finding for finding in CuratorChecks(self.root).run_record(record)
            if finding.rule == "CUR-WR-EARLY-CONVERGENCE"
            and finding.content_identifier == "early_convergence:resource_question"
        )

    def task_for_current_finding(self, **updates):
        finding = self.current_finding()
        value = {
            "task_id": "GKT-EARLY",
            "finding_id": finding.identifier,
            "durable_identity": (
                "CUR-WR-EARLY-CONVERGENCE|workflow_node|"
                "early_convergence:resource_question|workflow_reasoning_early_convergence"
            ),
            "status": "open", "owner": "Workflow Designer",
            "priority": "Medium", "classification": "Opportunity",
            "review_disposition": "NOT_REVIEWED",
            "finding_type": "workflow_reasoning_early_convergence",
            "content_type": "workflow_node",
            "content_identifier": "early_convergence:resource_question",
            "curator_rule": "CUR-WR-EARLY-CONVERGENCE",
            "related_workflows": ["early_convergence"],
            "provenance": copy.deepcopy(finding.provenance),
            "evidence": ["Original evidence snapshot"],
            "current_evidence": list(finding.evidence),
            "structured_evidence": copy.deepcopy(finding.structured_evidence),
            "times_observed": 4, "last_seen": "2026-08-28T00:00:00+00:00",
            "trend": "recurring", "knowledge_debt_score": 7,
            "history": [], "resolution_history": [],
            "resolution_package": {"status": "draft_created"},
        }
        value.update(updates)
        return value

    def service(self, **values):
        return CuratorEarlyConvergenceStageBReconciliationService(
            self.root,
            now=lambda: datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc),
            **values,
        )

    def replace_task(self, task):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task}
        self.store.save(state)

    def current_task(self):
        return self.store.load()["tasks"]["GKT-EARLY"]

    def test_existing_finding_refreshes_still_detected_with_whole_workflow_scope(self):
        before = copy.deepcopy(self.current_task())
        with patch.object(
            CuratorTargetedVerificationService, "verify",
            side_effect=AssertionError("mutating verifier must not run"),
        ):
            result = self.service().run(task_id="GKT-EARLY")
        after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "still_detected")
        self.assertEqual(
            after["current_verification"]["affected_fingerprint_scope"],
            "whole_workflow",
        )
        self.assertEqual(
            after["last_verified_fingerprint"],
            CuratorTargetedVerificationService.fingerprint(self.workflow()),
        )
        self.assert_preserved(before, after)

    def test_corrected_and_downstream_only_change_refreshes_new_result(self):
        first = self.service().run(task_id="GKT-EARLY")
        origin_before = copy.deepcopy(self.workflow()["nodes"]["resource_question"])
        self.write_workflow(self.workflow(corrected=True))
        second = self.service().run(task_id="GKT-EARLY")
        after = self.current_task()
        self.assertEqual(second.task_results[0].status, "COMMITTED")
        self.assertEqual(after["current_verification"]["status"], "appears_corrected")
        self.assertEqual(
            self.workflow(corrected=True)["nodes"]["resource_question"], origin_before
        )
        self.assertNotEqual(
            first.task_results[0].idempotency_key,
            second.task_results[0].idempotency_key,
        )
        self.assertEqual(len(after["history"]), 2)

    def test_missing_workflow_node_nonquestion_and_ambiguous_lifecycle_skip(self):
        path = self.root / "app/workflow_drafts/early_convergence.json"
        path.unlink()
        missing_workflow = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(missing_workflow.task_results[0].status, "SKIPPED")

        workflow = self.workflow()
        workflow["nodes"].pop("resource_question")
        self.write_workflow(workflow)
        missing_node = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(missing_node.task_results[0].status, "SKIPPED")

        self.write_workflow(self.workflow(origin_type="instruction"))
        nonquestion = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(nonquestion.task_results[0].status, "SKIPPED")
        self.assertIn("no longer a question", nonquestion.task_results[0].reason)

        self.write_workflow(self.workflow(), "one.json")
        self.write_workflow(self.workflow(), "two.json")
        ambiguous = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(ambiguous.task_results[0].status, "SKIPPED")

    def test_wrong_identity_provenance_and_nonactionable_tasks_skip(self):
        cases = (
            {"curator_rule": "OTHER-RULE"},
            {"finding_type": "other_finding"},
            {"content_type": "workflow"},
            {"status": "resolved"},
            {"status": "deferred"},
            {"content_identifier": "early_convergence"},
            {"content_identifier": "early_convergence:resource_question:extra"},
            {"related_workflows": ["other_workflow"]},
            {"provenance": {"workflow_id": "other_workflow"}},
            {"provenance": {"node_id": "other_node"}},
            {"provenance": {"source_path": "other.json"}},
            {"provenance": "malformed"},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                task = self.task_for_current_finding(**updates)
                self.replace_task(task)
                before = copy.deepcopy(self.current_task())
                result = self.service().run(task_id="GKT-EARLY")
                self.assertEqual(result.task_results[0].status, "SKIPPED")
                self.assertEqual(self.current_task(), before)

    def test_multiple_matches_and_finding_id_mismatch_fail_closed(self):
        finding = self.current_finding()
        service = self.service()
        with patch.object(service.checks, "run_record", return_value=[finding, finding]):
            multiple = service.run(task_id="GKT-EARLY")
        self.assertEqual(multiple.task_results[0].status, "SKIPPED")
        task = self.current_task()
        task["finding_id"] = "CUR-MISMATCH"
        self.replace_task(task)
        mismatch = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(mismatch.task_results[0].status, "SKIPPED")

    def test_same_key_does_not_rewrite_memory_or_duplicate_history(self):
        first = self.service().run(task_id="GKT-EARLY")
        memory_before = (self.root / "curation_memory/memory.json").read_bytes()
        history_before = copy.deepcopy(self.current_task()["history"])
        second = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(second.task_results[0].status, "SKIPPED")
        self.assertEqual(first.task_results[0].idempotency_key,
                         second.task_results[0].idempotency_key)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(),
                         memory_before)
        self.assertEqual(self.current_task()["history"], history_before)

    def test_dry_run_reports_exact_delta_and_writes_nothing(self):
        before = self.files()
        result = self.service().run(task_id="GKT-EARLY", dry_run=True)
        delta = result.task_results[0].proposed_delta
        self.assertEqual(result.task_results[0].status, "DRY_RUN")
        self.assertEqual(delta["capability"], {
            "id": "cur-wr-early-convergence-verification-refresh", "version": 1,
        })
        self.assertEqual(delta["identity"]["workflow_id"], "early_convergence")
        self.assertEqual(
            delta["identity"]["originating_question_node_id"], "resource_question"
        )
        self.assertEqual(delta["verification_result"], "still_detected")
        self.assertEqual(
            set(delta["changed_fields"]),
            {"current_verification", "last_verified_fingerprint", "history"},
        )
        self.assertTrue(delta["unchanged"]["evidence"])
        self.assertTrue(delta["unchanged"]["packages"])
        self.assertTrue(delta["unchanged"]["repair_authority"])
        self.assertEqual(self.files(), before)

    def test_only_verification_fields_change_and_external_state_is_untouched(self):
        protected = []
        for relative in (
            "app/workflow_drafts/early_convergence.json",
            "app/workflow_publications/current.json",
            "knowledge_base/published/article.json",
            "curation_memory/resolution_packages/GKT-EARLY.json",
            "curation_memory/structural_repair_approvals/sentinel.json",
            "curation_memory/structural_repair_applications/sentinel.json",
            "curation_memory/structural_repair_recoveries/sentinel.json",
            "curation_memory/fix_sessions/sentinel.json",
        ):
            path = self.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sentinel", encoding="utf-8")
            protected.append(path)
        external_before = {str(path): path.read_bytes() for path in protected}
        task_before = copy.deepcopy(self.current_task())
        result = self.service().run(task_id="GKT-EARLY")
        task_after = self.current_task()
        self.assertEqual(result.task_results[0].status, "COMMITTED")
        changed = {
            field for field in set(task_before) | set(task_after)
            if task_before.get(field) != task_after.get(field)
        }
        self.assertEqual(
            changed,
            {"current_verification", "last_verified_fingerprint", "history"},
        )
        self.assert_preserved(task_before, task_after)
        self.assertEqual(
            {str(path): path.read_bytes() for path in protected}, external_before
        )

    def test_shared_controls_lock_cas_and_crash_recovery_remain_bounded(self):
        state = self.store.load()
        state["controls"]["global_disabled"] = True
        self.store.save(state)
        disabled = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(disabled.task_results[0].status, "FAILED")
        state = self.store.load()
        state["controls"]["global_disabled"] = False
        self.store.save(state)
        with self.store.locked():
            locked = self.service(lock_timeout=0.01).run(task_id="GKT-EARLY")
        self.assertEqual(locked.task_results[0].status, "FAILED")

        def change_owner(plan, attempt):
            if attempt == 0:
                current = self.store.load()
                current["tasks"]["GKT-EARLY"]["owner"] = "Human"
                self.store.save(current)

        service = self.service()
        service._before_commit = change_owner
        committed = service.run(task_id="GKT-EARLY")
        self.assertEqual(committed.task_results[0].status, "COMMITTED")
        self.assertEqual(self.current_task()["owner"], "Human")

        self.write_workflow({**self.workflow(), "name": "Changed for crash test"})
        before = (self.root / "curation_memory/memory.json").read_bytes()
        with patch.object(
            LockedCuratorMemory, "compare_and_swap",
            side_effect=RuntimeError("simulated crash before commit"),
        ):
            failed_before = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(failed_before.task_results[0].status, "FAILED")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

        service = self.service()
        append = service.journal.append

        def crash_after(event):
            if event.status == "COMMITTED":
                raise StageBJournalError("simulated journal interruption")
            return append(event)

        service.journal.append = crash_after
        failed_after = service.run(task_id="GKT-EARLY")
        self.assertEqual(failed_after.task_results[0].status, "FAILED")
        recovered = self.service().run(task_id="GKT-EARLY")
        self.assertEqual(recovered.task_results[0].status, "COMMITTED")
        self.assertIn("Recovered", recovered.task_results[0].reason)

    def test_explicit_cli_and_existing_capability_contracts_are_unchanged(self):
        before = self.files()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "refresh-early-convergence-verification",
                "--repository", str(self.root), "--task-id", "GKT-EARLY",
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn(
            '"capability_id": "cur-wr-early-convergence-verification-refresh"',
            output.getvalue(),
        )
        self.assertEqual(self.files(), before)
        self.assertEqual(
            CuratorStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-progress-verification-refresh",
        )
        self.assertEqual(
            CuratorTerminalEvidenceStageBReconciliationService.CAPABILITY_ID,
            "cur-wr-terminal-evidence-verification-refresh",
        )
        self.assertEqual(
            CuratorTerminalEvidenceCurrentEvidenceSyncService.CAPABILITY_ID,
            "cur-wr-terminal-evidence-current-evidence-sync",
        )
        self.assertEqual(
            CuratorEarlyConvergenceStageBReconciliationService.MUTATION_FIELDS,
            ("current_verification", "last_verified_fingerprint", "history"),
        )

    def assert_preserved(self, before, after):
        for field in (
            "status", "owner", "priority", "classification",
            "review_disposition", "evidence", "current_evidence",
            "structured_evidence", "last_seen", "times_observed", "trend",
            "knowledge_debt_score", "resolution_package", "finding_id",
            "provenance", "recommended_action", "resolution_history",
        ):
            self.assertEqual(after.get(field), before.get(field), field)

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
