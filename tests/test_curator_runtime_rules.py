import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from curator.auditor import CuratorAuditor
from curator.checks import CuratorChecks
from curator.growth import CuratorGrowthError, CuratorGrowthService
from curator.memory import CuratorMemoryStore
from curator.models import InventoryRecord
from curator.runtime_rules import runtime_rule_fingerprint
from curator.shadow_rules import run_level_one_shadow


MANIFEST = {
    "schema_version": "1.0",
    "rule_id": "CUR-SAFE-L1",
    "variant": "proportional_safety_hierarchy_v1",
    "parameters": {"accept_stronger_levels": True},
}


class CuratorRuntimeRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.growth = CuratorGrowthService(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def _record(self, instruction="Save any work in the application before restarting it."):
        workflow = {
            "workflow_id": "flow", "name": "Flow", "category": "Desktop Support",
            "platform": "Windows", "start_node": "restart",
            "nodes": {"restart": {"type": "instruction", "title": "Restart Application",
                                    "instruction": instruction, "next": "done"},
                      "done": {"type": "resolution", "title": "Done", "message": "Complete."}},
        }
        return InventoryRecord("workflow", "flow", "Flow", "flow.json",
                               "Desktop Support", "Windows", "draft", workflow)

    def _proposal(self):
        return self.growth.propose("audit_rule", {
            "proposed_capability": "Accept stronger proportional safety guidance",
            "problem_addressed": "CUR-SAFE-L1 can reject valid save-work guidance.",
            "supporting_task_ids": ["GKT-ONE"], "recurrence_count": 3,
            "expected_benefit": "Remove a deterministic false positive.",
            "scope": "CUR-SAFE-L1 only", "required_tools": [],
            "required_permissions": ["read_trusted_content", "write_audit_output"],
            "risks": ["Incorrectly accepting unrelated save language"],
            "test_plan": ["Run the fixed shadow matrix"],
            "rollback_plan": "Suspend the rule.", "confidence": "high",
            "rule_id": "CUR-SAFE-L1", "runtime_rule": deepcopy(MANIFEST),
        })

    def _activate(self):
        proposal = self._proposal()
        proposal = self.growth.decide_proposal(proposal["proposal_id"], "test_only",
                                               reviewer="Greg", reason="Authorize shadow test.")
        self.growth.record_shadow_result(proposal["proposal_id"], run_level_one_shadow([
            {"name": "bare", "node": {"instruction": "Restart the application."},
             "expected_finding": True},
            {"name": "stronger", "node": {"instruction": "Save any work before restarting it."},
             "expected_finding": False},
        ]))
        self.growth.decide_proposal(proposal["proposal_id"], "human_approved",
                                    reviewer="Greg", reason="Shadow evidence reviewed.")
        return self.growth.decide_proposal(proposal["proposal_id"], "active",
                                           reviewer="Greg", reason="Activate registered rule.")

    @staticmethod
    def _safety(findings):
        return [item for item in findings if item.rule == "CUR-SAFE-L1"]

    def test_inactive_proposal_does_not_change_runtime_and_active_rule_does(self):
        proposal = self._proposal()
        before = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual(len(self._safety(before)), 1)
        self.growth.decide_proposal(proposal["proposal_id"], "test_only",
                                    reviewer="Greg", reason="Shadow only.")
        test_only = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual(len(self._safety(test_only)), 1)

        # Complete the governed lifecycle and verify production semantics change.
        self.growth.record_shadow_result(proposal["proposal_id"], run_level_one_shadow([
            {"name": "bare", "node": {"instruction": "Restart the app."}, "expected_finding": True},
            {"name": "save", "node": {"instruction": "Save work before restarting."},
             "expected_finding": False},
        ]))
        self.growth.decide_proposal(proposal["proposal_id"], "human_approved",
                                    reviewer="Greg", reason="Evidence accepted.")
        approved_only = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual(len(self._safety(approved_only)), 1)
        self.growth.decide_proposal(proposal["proposal_id"], "active",
                                    reviewer="Greg", reason="Activate.")
        after = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual(self._safety(after), [])
        self.assertEqual(len(self._safety(CuratorChecks(self.root).run_record(
            self._record("Restart the application.")))), 1)

    def test_active_rule_fails_closed_for_irrelevant_or_ambiguous_save_and_preserves_levels_two_three(self):
        self._activate()
        checks = CuratorChecks(self.root)
        for wording in ("Restart the application and save the report afterward.",
                        "Save settings and restart the application."):
            self.assertEqual(len(self._safety(checks.run_record(self._record(wording)))), 1)
        level_two = self._record("Restart Windows.")
        self.assertTrue(any(item.rule == "CUR-SAFE-L2" for item in checks.run_record(level_two)))
        protected_two = self._record("Save active work before restarting Windows.")
        self.assertFalse(any(item.rule == "CUR-SAFE-L2" for item in checks.run_record(protected_two)))
        level_three = self._record("Run System Restore.")
        self.assertTrue(any(item.rule == "CUR-SAFE-L3" for item in checks.run_record(level_three)))
        protected_three = self._record("Create a restore point before running System Restore.")
        self.assertFalse(any(item.rule == "CUR-SAFE-L3" for item in checks.run_record(protected_three)))

    def test_active_rule_accepts_open_work_only_before_the_disruptive_action(self):
        self._activate()
        checks = CuratorChecks(self.root)
        compliant = self._record(
            "Save any open work before restarting the application. Restart the application "
            "and repeat the action that previously caused the failure."
        )
        self.assertEqual(self._safety(checks.run_record(compliant)), [])

        for instruction in (
            "Restart the application, then save any open work.",
            "Save the application settings before restarting the application.",
            "Restart the application and repeat the action that caused the failure.",
        ):
            with self.subTest(instruction=instruction):
                self.assertEqual(len(self._safety(checks.run_record(self._record(instruction)))), 1)

    def test_activation_records_immutable_manifest_provenance_and_tampering_fails_closed(self):
        active = self._activate()
        self.assertEqual(active["activated_runtime_rule"], MANIFEST)
        self.assertEqual(active["activation"]["manifest_fingerprint"],
                         runtime_rule_fingerprint(MANIFEST))
        state = self.store.load()
        stored = state["growth"]["proposals"][active["proposal_id"]]
        stored["activated_runtime_rule"]["parameters"]["accept_stronger_levels"] = False
        self.store.save(state)
        findings = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual(len(self._safety(findings)), 1)

    def test_unknown_runtime_variant_cannot_activate(self):
        proposal = self._proposal()
        state = self.store.load()
        state["growth"]["proposals"][proposal["proposal_id"]]["runtime_rule"]["variant"] = "python_expression"
        self.store.save(state)
        proposal = self.growth.decide_proposal(proposal["proposal_id"], "test_only",
                                               reviewer="Greg", reason="Shadow test.")
        self.growth.record_shadow_result(proposal["proposal_id"], run_level_one_shadow([
            {"name": "bare", "node": {"instruction": "Restart the app."},
             "expected_finding": True},
        ]))
        self.growth.decide_proposal(proposal["proposal_id"], "human_approved",
                                    reviewer="Greg", reason="Review complete.")
        with self.assertRaises(CuratorGrowthError):
            self.growth.decide_proposal(proposal["proposal_id"], "active",
                                        reviewer="Greg", reason="Attempt activation.")

    def test_suspension_disables_rule_without_restart_and_repeated_checks_are_idempotent(self):
        active = self._activate()
        snapshot = deepcopy(self.store.load()["growth"]["proposals"][active["proposal_id"]])
        first = CuratorChecks(self.root).run_record(self._record())
        second = CuratorChecks(self.root).run_record(self._record())
        self.assertEqual([item.identifier for item in first], [item.identifier for item in second])
        self.assertEqual(self.store.load()["growth"]["proposals"][active["proposal_id"]], snapshot)
        self.growth.decide_proposal(active["proposal_id"], "suspended",
                                    reviewer="Greg", reason="Rollback test.")
        self.assertEqual(len(self._safety(CuratorChecks(self.root).run_record(self._record()))), 1)

    def test_full_audit_and_targeted_verification_consume_same_active_rule(self):
        self._activate()
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        record = self._record()
        (drafts / "flow.json").write_text(json.dumps(record.raw), encoding="utf-8")
        state = self.store.load()
        state["tasks"]["GKT-RUNTIME"] = {
            "task_id": "GKT-RUNTIME", "content_type": "workflow_node",
            "content_identifier": "flow:restart", "curator_rule": "CUR-SAFE-L1",
            "finding_type": "missing_safety_guidance", "classification": "risk",
            "status": "open", "evidence": ["Original finding"], "history": [],
        }
        self.store.save(state)
        audit, _ = CuratorAuditor(self.root, self.root / "runs",
                                  self.root / "curation_memory").audit(write=False)
        self.assertFalse(any(item.rule == "CUR-SAFE-L1" and
                             item.content_identifier == "flow:restart" for item in audit.findings))
        targeted = CuratorTargetedVerificationService(self.root).verify("GKT-RUNTIME")
        self.assertEqual(targeted["status"], "appears_corrected")
        self.assertEqual(self.store.load()["tasks"]["GKT-RUNTIME"]["status"], "open")

    def test_targeted_verification_marks_open_work_guidance_corrected(self):
        self._activate()
        drafts = self.root / "app" / "workflow_drafts"
        drafts.mkdir(parents=True)
        record = self._record(
            "Save any open work before restarting the application. Restart the application "
            "and repeat the action that previously caused the failure."
        )
        (drafts / "flow.json").write_text(json.dumps(record.raw), encoding="utf-8")
        state = self.store.load()
        state["tasks"]["GKT-OPEN-WORK"] = {
            "task_id": "GKT-OPEN-WORK", "content_type": "workflow_node",
            "content_identifier": "flow:restart", "curator_rule": "CUR-SAFE-L1",
            "finding_type": "missing_safety_guidance", "classification": "risk",
            "status": "open", "evidence": ["Restart the application."], "history": [],
        }
        self.store.save(state)

        targeted = CuratorTargetedVerificationService(self.root).verify("GKT-OPEN-WORK")

        self.assertEqual(targeted["status"], "appears_corrected")
        self.assertEqual(self.store.load()["tasks"]["GKT-OPEN-WORK"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
