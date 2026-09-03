import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.services.curator_growth_service import CuratorGrowthService as AppCuratorGrowthService
from curator.governance import CuratorGovernanceError, CuratorGovernancePolicy, PROFILES
from curator.growth import CuratorGrowthError, CuratorGrowthService
from curator.memory import CuratorMemoryStore
from curator.shadow_rules import proposed_proportional_safety, run_level_one_shadow
from curator.tasks import KnowledgeTaskService


class CuratorGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CuratorMemoryStore(Path(self.temporary.name) / "curation_memory")
        self.service = CuratorGrowthService(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def proposal_data(**overrides):
        value = {
            "proposed_capability": "Detect stale aliases",
            "problem_addressed": "Repeated stale alias findings",
            "supporting_task_ids": ["GKT-ONE"],
            "recurrence_count": 3,
            "expected_benefit": "Less mechanical review",
            "scope": "Knowledge identity aliases only",
            "required_tools": ["canonical_index"],
            "required_permissions": ["read_trusted_content", "write_audit_output"],
            "risks": ["False positive alias matches"],
            "test_plan": ["Run in shadow mode", "Compare to prior audits"],
            "rollback_plan": "Retire the proposal and preserve its history.",
            "confidence": "medium",
        }
        value.update(overrides)
        return value

    def test_policy_is_code_owned_and_least_privilege(self):
        self.assertTrue(CuratorGovernancePolicy.snapshot()["immutable"])
        self.assertNotIn("publish_content", PROFILES["audit"].permissions)
        with self.assertRaises(CuratorGovernanceError):
            CuratorGovernancePolicy.authorize("audit", "publish_content")

    def test_old_memory_is_backfilled_without_migration(self):
        state = self.store.load()
        state.pop("growth")
        state.pop("controls")
        self.store.save(state)
        loaded = self.store.load()
        self.assertEqual(loaded["growth"]["event_queue"], [])
        self.assertTrue(loaded["controls"]["scheduled_runs_disabled"])
        self.assertTrue(loaded["controls"]["stage_b_scheduled_runs_disabled"])

    def test_proposal_is_human_gated_and_cannot_skip_lifecycle(self):
        proposal = self.service.propose("audit_rule", self.proposal_data())
        self.assertEqual(proposal["status"], "proposed")
        self.assertTrue(proposal["human_gate"])
        with self.assertRaises(CuratorGrowthError):
            self.service.decide_proposal(proposal["proposal_id"], "active",
                                         reviewer="Greg", reason="Not shadow tested.")
        with self.assertRaisesRegex(CuratorGrowthError, "automated identities"):
            self.service.decide_proposal(proposal["proposal_id"], "test_only",
                                         reviewer="Curator", reason="Self approval")
        updated = self.service.decide_proposal(proposal["proposal_id"], "test_only",
                                               reviewer="Greg", reason="Begin shadow comparison.")
        self.assertEqual(updated["status"], "test_only")

    def test_shadow_results_never_activate_a_rule(self):
        proposal = self.service.propose("audit_rule", self.proposal_data())
        proposal = self.service.decide_proposal(proposal["proposal_id"], "test_only",
                                                reviewer="Greg", reason="Shadow test approved.")
        proposal = self.service.record_shadow_result(proposal["proposal_id"], {
            "findings": 10, "false_positives": 3, "affected_content": ["article:one"]
        })
        self.assertEqual(proposal["status"], "test_only")
        dashboard = self.service.dashboard()
        self.assertTrue(dashboard["proposals"][0]["needs_review"])

    def test_rule_evaluation_is_structured_and_idempotent(self):
        data = {
            "rule_id": "CUR-SAFE-L1", "rule_fingerprint": "rule-hash",
            "content_identifier": "workflow:node", "content_fingerprint": "content-hash",
            "expected_behavior": "Accept stronger safety guidance.",
            "actual_behavior": "Finding remains.", "outcome": "candidate_false_positive",
            "evidence": ["Contains save-work guidance."], "task_id": "GKT-ONE",
            "maintenance_session_id": "CFX-ONE",
        }
        first = self.service.record_evaluation(data)
        second = self.service.record_evaluation(data)
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(len(self.service.dashboard()["evaluations"]), 1)

    def test_level_one_shadow_accepts_stronger_guidance_and_detects_missing_guidance(self):
        self.assertTrue(proposed_proportional_safety(
            {"instruction": "Save any work before restarting the application."}, 1))
        result = run_level_one_shadow([
            {"name": "missing", "node": {"instruction": "Restart the application."},
             "expected_finding": True},
            {"name": "level-one", "node": {"instruction": "Close and reopen the application."},
             "expected_finding": False},
            {"name": "stronger", "node": {"instruction": "Save active work, then restart the application."},
             "expected_finding": False},
        ])
        self.assertTrue(result["passed"])
        self.assertEqual(result["confusion_matrix"]["false_positive"], 0)

    def test_safety_shadow_covers_edge_cases_and_preserves_higher_levels(self):
        result = run_level_one_shadow([
            {"name": "bare restart", "node": {"instruction": "Restart the app."},
             "expected_finding": True},
            {"name": "close and reopen", "node": {"instruction": "Close and reopen the application."},
             "expected_finding": False},
            {"name": "save work", "node": {"instruction": "Save your work before restarting."},
             "expected_finding": False},
            {"name": "unsaved work impact", "node": {"warning": "You may lose unsaved work."},
             "expected_finding": False},
            {"name": "convenient but vague", "node": {"instruction": "Restart when convenient."},
             "expected_finding": True},
            {"name": "unrelated save", "node": {"instruction": "Restart the app and save the report afterward."},
             "expected_finding": True},
            {"name": "higher impact remains protected", "level": 2,
             "node": {"instruction": "Restart Windows."}, "expected_finding": True},
            {"name": "higher impact proportional", "level": 2,
             "node": {"instruction": "Save active work before restarting Windows."},
             "expected_finding": False},
            {"name": "ambiguous save reference", "node": {"instruction": "Save settings and restart."},
             "expected_finding": None},
        ])
        self.assertTrue(result["passed"])
        self.assertEqual(result["uncertain"], 1)
        self.assertEqual(result["confusion_matrix"], {
            "true_positive": 4, "true_negative": 4,
            "false_positive": 0, "false_negative": 0,
        })

    def test_growth_ui_renders_rule_evidence_shadow_matrix_and_human_gate(self):
        proposal = self.service.propose("audit_rule", self.proposal_data(
            proposed_capability="Calibrate CUR-SAFE-L1",
            rule_id="CUR-SAFE-L1", proposed_behavior="Recognize proportional guidance.",
            supporting_evidence=["CGEV-ONE"],
        ))
        proposal = self.service.decide_proposal(proposal["proposal_id"], "test_only",
                                                reviewer="Greg", reason="Shadow only.")
        self.service.record_shadow_result(proposal["proposal_id"], run_level_one_shadow([
            {"name": "bare restart", "node": {"instruction": "Restart the app."},
             "expected_finding": True},
            {"name": "save work", "node": {"instruction": "Save work before restarting."},
             "expected_finding": False},
        ]))
        dashboard = self.service.dashboard()
        facade = Mock()
        facade.dashboard.return_value = dashboard
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorGrowthService", return_value=facade):
            with flask_app.test_client() as client:
                response = client.get("/curator/growth")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CUR-SAFE-L1", response.data)
        self.assertIn(b"TP 1", response.data)
        self.assertIn(b"TN 1", response.data)
        self.assertIn(b"separate human approval", response.data)

    def test_growth_ui_humanizes_reasoning_calibration_heading_and_preserves_identity(self):
        raw_identity = (
            "reasoning_calibration:cur-wr-early-convergence:rcp-1234abcd:useful"
        )
        lesson = self.service.record_lesson({
            "pattern_observed": raw_identity,
            "supporting_evidence": ["GKT-ONE", "GKT-TWO"],
            "recommended_future_behavior": "Keep this calibration under human review.",
        })
        dashboard = AppCuratorGrowthService(Path(self.temporary.name)).dashboard()
        presented = next(item for item in dashboard["lessons"]
                         if item["lesson_id"] == lesson["lesson_id"])
        self.assertEqual(presented["display_category"], "Reasoning Calibration")
        self.assertEqual(presented["display_title"], "Early Branch Convergence")
        self.assertEqual(presented["rule_id"], "CUR-WR-EARLY-CONVERGENCE")
        self.assertEqual(presented["calibration_id"], "RCP-1234ABCD")
        self.assertEqual(presented["raw_identity"], raw_identity)
        self.assertEqual(
            AppCuratorGrowthService._present_lesson({
                "pattern_observed": "unknown_machine-value",
            })["display_title"],
            "Unknown Machine Value",
        )

        facade = Mock()
        facade.dashboard.return_value = dashboard
        flask_app.config.update(TESTING=True)
        with patch("app.app.CuratorGrowthService", return_value=facade):
            with flask_app.test_client() as client:
                page = client.get("/curator/growth").get_data(as_text=True)

        self.assertIn("<h3>Early Branch Convergence</h3>", page)
        self.assertIn("Reasoning Calibration", page)
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", page)
        self.assertIn("RCP-1234ABCD", page)
        self.assertIn(lesson["lesson_id"], page)
        self.assertIn(raw_identity, page)
        self.assertNotIn(f"<h3>{raw_identity}</h3>", page)

    def test_failed_shadow_blocks_audit_rule_approval_and_pass_still_needs_human_steps(self):
        proposal = self.service.propose("audit_rule", self.proposal_data())
        proposal = self.service.decide_proposal(proposal["proposal_id"], "test_only",
                                                reviewer="Greg", reason="Shadow testing authorized.")
        proposal = self.service.record_shadow_result(proposal["proposal_id"], {
            "passed": False, "findings": 1, "false_positives": 1,
            "confusion_matrix": {"false_positive": 1},
        })
        with self.assertRaisesRegex(CuratorGrowthError, "shadow test passes"):
            self.service.decide_proposal(proposal["proposal_id"], "human_approved",
                                         reviewer="Greg", reason="Should remain blocked.")
        self.assertEqual(self.service.dashboard()["proposals"][0]["status"], "test_only")

        passing = self.service.propose("audit_rule", self.proposal_data(
            proposed_capability="Detect a second safety pattern"))
        passing = self.service.decide_proposal(passing["proposal_id"], "test_only",
                                               reviewer="Greg", reason="Shadow testing authorized.")
        passing = self.service.record_shadow_result(passing["proposal_id"], {
            "passed": True, "findings": 1,
            "confusion_matrix": {"true_positive": 1},
        })
        self.assertEqual(passing["status"], "test_only")
        approved = self.service.decide_proposal(passing["proposal_id"], "human_approved",
                                                reviewer="Greg", reason="Shadow evidence reviewed.")
        self.assertEqual(approved["status"], "human_approved")
        self.assertNotEqual(approved["status"], "active")

    def test_adapter_preserves_sandbox_evidence_and_cannot_skip_stages(self):
        proposal = self.service.propose("repair_adapter", self.proposal_data(
            proposed_capability="Normalize safe source delimiters",
            eligibility_conditions=["Exactly one authoritative URL"],
            before_after={"before": "title || url", "after": "title | url"},
            affected_file_types=["json"], audit_logging={"required": True},
        ))
        self.assertEqual(proposal["affected_file_types"], ["json"])
        with self.assertRaises(CuratorGrowthError):
            self.service.decide_proposal(proposal["proposal_id"], "enabled",
                                         reviewer="Greg", reason="Cannot skip sandbox stages.")

    def test_lessons_do_not_change_policy_and_require_a_human(self):
        lesson = self.service.record_lesson({
            "pattern_observed": "Malformed source delimiters recur",
            "supporting_evidence": ["GKT-ONE", "GKT-TWO"],
            "recommended_future_behavior": "Propose a deterministic rule.",
        })
        with self.assertRaises(CuratorGrowthError):
            self.service.decide_lesson(lesson["lesson_id"], "approved",
                                       reviewer="system", reason="Automated approval")
        approved = self.service.decide_lesson(lesson["lesson_id"], "approved",
                                              reviewer="Greg", reason="Evidence reviewed.")
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(CuratorGovernancePolicy.snapshot()["immutable"])

    def test_global_disable_blocks_operations_but_human_can_restore_control(self):
        self.service.set_control("global_disabled", True, reviewer="Greg", reason="Emergency stop.")
        with self.assertRaises(CuratorGovernanceError):
            self.service.propose("audit_rule", self.proposal_data())
        controls = self.service.set_control("global_disabled", False, reviewer="Greg",
                                            reason="Incident reviewed.")
        self.assertFalse(controls["global_disabled"])

    def test_human_can_govern_stage_b_scheduled_runs_independently(self):
        controls = self.service.set_control(
            "stage_b_scheduled_runs_disabled", False,
            reviewer="Greg", reason="Approved bounded scheduled rollout.",
        )
        self.assertFalse(controls["stage_b_scheduled_runs_disabled"])
        self.assertTrue(controls["scheduled_runs_disabled"])

    def test_event_queue_is_targeted_and_never_requests_broad_sync(self):
        event = self.service.enqueue_event("workflow_changed", "workflow:printer",
                                           actor="Workflow Designer")
        self.assertEqual(event["requested_operation"], "targeted_audit")
        self.assertFalse(event["broad_sync"])

    def test_execution_modes_remain_conservative(self):
        self.assertEqual(KnowledgeTaskService.execution_mode({"classification": "Risk"}), "ASSISTED")
        self.assertEqual(KnowledgeTaskService.execution_mode({
            "classification": "Recommendation", "finding_type": "taxonomy_improvement"
        }), "HUMAN_DECISION")
        self.assertEqual(KnowledgeTaskService.execution_mode({"classification": "Defect"}), "ASSISTED")


if __name__ == "__main__":
    unittest.main()
