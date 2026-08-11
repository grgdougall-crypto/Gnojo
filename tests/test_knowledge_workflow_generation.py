import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.services.knowledge_workflow_generation_service import (
    KnowledgeWorkflowGenerationError,
    KnowledgeWorkflowGenerationService,
)


class KnowledgeWorkflowGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in (
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
            "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/commands",
            "knowledge_campaigns/claim_planning", "knowledge_campaigns/research",
            "knowledge_campaigns/evidence_extraction",
        ):
            (self.root / directory).mkdir(parents=True)
        self.campaign_root = self.root / "knowledge_campaigns"
        self.campaign_id = "KCC-WORKFLOW01"
        self.work_id = "KCW-WORKFLOW01"
        self.campaign = {
            "schema_version": "1.0", "campaign_id": self.campaign_id, "title": "Connectivity",
            "status": "analyzed", "last_analyzed_at": "now", "category": "Networking",
            "platforms": ["Windows"], "gaps": [{"gap_id": "KCG-WORKFLOW01"}],
            "work_items": [{"work_item_id": self.work_id, "campaign_id": self.campaign_id,
                "gap_id": "KCG-WORKFLOW01", "work_type": "workflow", "area_id": "browser-connectivity",
                "target_asset": "browser_connectivity", "priority": "high", "confidence": "high",
                "dependencies": [], "status": "proposed"}],
        }
        self._write(self.campaign_root / f"{self.campaign_id}.json", self.campaign)
        self._write(self.root / "app/decision_trees/higher_layer.json", {
            "workflow_id": "higher_layer", "name": "Higher Layer", "start_node": "done",
            "nodes": {"done": {"type": "resolution", "title": "Done", "message": "Reviewed."}},
        })
        self._write_claim_plan(self._claims())
        self.service = KnowledgeWorkflowGenerationService(
            self.root, self.campaign_root, self.root / "app/workflow_drafts"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _spec(self, claim_id, node_id, node_type, fields, **extra):
        return {"claim_id": claim_id, "normalized_claim": fields.get("question") or fields.get("instruction")
                or fields.get("message") or fields.get("title"), "review_state": "approved", "stale": False,
                "evidence_ids": [f"EVD-{claim_id}"], "source_urls": ["https://learn.microsoft.com/windows"],
                "workflow_spec": {"node_id": node_id, "type": node_type, "fields": fields, **extra}}

    def _claims(self):
        return [
            self._spec("CLM-QSTART", "q_start", "question", {"question": "Does the browser reach example.com?",
                "help_text": "This separates browser access from a wider connectivity failure.", "answers": {
                    "yes": {"label": "Yes", "next": "r_done"},
                    "no": {"label": "No", "next": "i_restart"},
                    "unsure": {"label": "I'm Not Sure", "next": "r_escalate"},
                }}, start_node="q_start", workflow_name="Browser Connectivity", category="Networking", platform="Windows"),
            self._spec("CLM-ACTION", "i_restart", "instruction", {"title": "Restart the Browser",
                "instruction": "Save work, then restart the browser. This may interrupt active browser sessions.",
                "next": "q_verify"}),
            self._spec("CLM-VERIFY", "q_verify", "question", {"question": "Does the website load after restarting?",
                "answers": {"yes": {"label": "Yes", "next": "r_done"},
                            "no": {"label": "No", "next": "t_higher"},
                            "unsure": {"label": "I'm Not Sure", "next": "r_escalate"}}}),
            self._spec("CLM-DONE", "r_done", "resolution", {"title": "Browser Access Restored",
                "message": "The tested website loads after the browser check."}),
            self._spec("CLM-ESC", "r_escalate", "resolution", {"title": "Additional Evidence Is Needed",
                "message": "Record the website, browser, and observed error before escalating."}),
            self._spec("CLM-HANDOFF", "t_higher", "transition", {"title": "Continue Higher-Layer Diagnostics",
                "message": "Continue with approved higher-layer connectivity checks.", "next_workflow": "higher_layer"}),
        ]

    def _write_claim_plan(self, claims, status="ready_for_drafting"):
        self._write(self.campaign_root / "claim_planning/KCPM-WORKFLOW01.json", {
            "schema_version": "1.0", "claim_plan_id": "KCPM-WORKFLOW01",
            "campaign_id": self.campaign_id, "work_item_id": self.work_id,
            "status": status, "claims": claims,
        })

    def _planned(self):
        package = self.service.prepare(self.campaign_id, self.work_id)
        return self.service.plan(package["generation_id"])

    def _draft(self):
        package = self._planned()
        return self.service.prepare_draft(package["generation_id"])

    def test_explicit_human_initiation_is_required(self):
        self.assertEqual(self.service.list_for_campaign(self.campaign_id), [])
        self.assertFalse((self.campaign_root / "workflow_generation").glob("KWG-*.json").__iter__().__next__()
                         if list((self.campaign_root / "workflow_generation").glob("KWG-*.json")) else False)

    def test_eligibility_requires_analyzed_proposed_workflow_and_approved_structured_claims(self):
        self.assertTrue(self.service.eligibility(self.campaign_id, self.work_id)["eligible"])
        plan_path = self.campaign_root / "claim_planning/KCPM-WORKFLOW01.json"
        plan = json.loads(plan_path.read_text())
        plan["claims"][0].pop("workflow_spec")
        self._write(plan_path, plan)
        gate = self.service.eligibility(self.campaign_id, self.work_id)
        self.assertFalse(gate["eligible"])
        self.assertIn("structured workflow specification", " ".join(gate["reasons"]))

    def test_prepare_has_stable_kwg_identity_and_is_idempotent(self):
        first = self.service.prepare(self.campaign_id, self.work_id)
        second = self.service.prepare(self.campaign_id, self.work_id)
        self.assertRegex(first["generation_id"], r"^KWG-[A-F0-9]{12}$")
        self.assertEqual(first, second)
        self.assertEqual(first["intent"], "create")

    def test_canonical_workflow_forces_expand_and_create_is_blocked(self):
        canonical = {"workflow_id": "browser_connectivity", "name": "Browser Connectivity",
                     "start_node": "existing", "nodes": {"existing": {"type": "resolution",
                     "title": "Existing", "message": "Existing result."}}}
        self._write(self.root / "app/decision_trees/browser_connectivity.json", canonical)
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "Reuse or expand"):
            self.service.prepare(self.campaign_id, self.work_id, "create")
        package = self.service.prepare(self.campaign_id, self.work_id)
        self.assertEqual(package["intent"], "expand")

    def test_plan_precedes_draft_and_preserves_node_provenance(self):
        package = self.service.prepare(self.campaign_id, self.work_id)
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "plan first"):
            self.service.prepare_draft(package["generation_id"])
        package = self.service.plan(package["generation_id"])
        self.assertEqual(package["status"], "plan_ready")
        self.assertIsNone(package["workflow_draft"])
        draft = self.service.prepare_draft(package["generation_id"])
        provenance = draft["workflow_draft"]["nodes"]["i_restart"]["knowledge_factory"]
        self.assertEqual(provenance["claim_ids"], ["CLM-ACTION"])
        self.assertEqual(provenance["evidence_ids"], ["EVD-CLM-ACTION"])

    def test_plan_encodes_evidence_questions_uncertainty_verification_and_handoff(self):
        nodes = {item["node_id"]: item for item in self._planned()["workflow_plan"]["nodes"]}
        self.assertEqual(nodes["q_start"]["fields"]["answers"]["unsure"]["next"], "r_escalate")
        self.assertEqual(nodes["i_restart"]["fields"]["next"], "q_verify")
        self.assertEqual(nodes["t_higher"]["fields"]["next_workflow"], "higher_layer")

    def test_valid_draft_passes_structure_reasoning_safety_relationships(self):
        package = self._draft()
        self.assertEqual(package["status"], "draft_ready")
        self.assertFalse(any(item["level"] == "error" for item in package["validation_results"]))
        self.assertFalse(any(item["level"] == "error" for item in package["relationship_results"]))

    def test_unreachable_node_and_missing_relationship_block_draft(self):
        claims = self._claims()
        claims.append(self._spec("CLM-ORPHAN", "r_orphan", "resolution",
                                 {"title": "Orphan", "message": "Unreachable."}))
        claims[-1]["workflow_spec"]["fields"]["next_workflow"] = "missing"
        self._write_claim_plan(claims)
        package = self._draft()
        self.assertEqual(package["status"], "needs_revision")
        self.assertTrue(any(item["check"] == "reachability" and item["level"] == "error"
                            for item in package["validation_results"]))

    def test_loop_and_action_without_verification_do_not_reach_ready(self):
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"]["next"] = "q_start"
        self._write_claim_plan(claims)
        package = self._draft()
        self.assertEqual(package["status"], "needs_revision")

    def test_state_change_without_proportional_safety_is_blocked(self):
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"]["title"] = "Remove the Browser"
        claims[1]["workflow_spec"]["fields"]["instruction"] = "Uninstall the browser."
        self._write_claim_plan(claims)
        package = self._draft()
        self.assertTrue(any(item["check"] == "proportional_safety" and item["level"] == "error"
                            for item in package["validation_results"]))

    def test_missing_next_workflow_is_a_relationship_error(self):
        claims = self._claims()
        claims[-1]["workflow_spec"]["fields"]["next_workflow"] = "not_canonical"
        self._write_claim_plan(claims)
        package = self._draft()
        self.assertTrue(any(item["field"] == "next_workflow" and item["level"] == "error"
                            for item in package["relationship_results"]))

    def test_review_lifecycle_and_handoff_are_explicit_and_never_publish(self):
        package = self._draft()
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "approval"):
            self.service.handoff(package["generation_id"])
        package = self.service.review(package["generation_id"], "approved", "Reviewed")
        self.assertEqual(package["status"], "approved_for_handoff")
        first = self.service.handoff(package["generation_id"])
        second = self.service.handoff(package["generation_id"])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "handed_off")
        self.assertTrue((self.root / "app/workflow_drafts/browser_connectivity.json").exists())
        self.assertEqual(list((self.root / "app/workflow_publications").rglob("v*.json")), [])

    def test_reject_and_needs_revision_never_write_content_studio(self):
        for decision in ("rejected", "needs_revision"):
            package = self._draft()
            package = self.service.review(package["generation_id"], decision)
            with self.assertRaises(KnowledgeWorkflowGenerationError):
                self.service.handoff(package["generation_id"])
        self.assertEqual(list((self.root / "app/workflow_drafts").glob("browser_connectivity.json")), [])

    def test_changed_claim_marks_package_stale_and_reprepare_preserves_revision(self):
        first = self._planned()
        path = self.campaign_root / "claim_planning/KCPM-WORKFLOW01.json"
        plan = json.loads(path.read_text())
        plan["claims"][0]["workflow_spec"]["fields"]["help_text"] += " Record the result."
        self._write(path, plan)
        self.assertTrue(self.service.get(first["generation_id"])["stale"])
        second = self.service.prepare(self.campaign_id, self.work_id)
        self.assertEqual(second["generation_id"], first["generation_id"])
        self.assertEqual(len(second["revisions"]), 1)

    def test_expansion_delta_preserves_unchanged_canonical_nodes(self):
        canonical = {"workflow_id": "browser_connectivity", "name": "Browser Connectivity",
            "category": "Networking", "platform": "Windows", "start_node": "existing",
            "nodes": {"existing": {"type": "instruction", "title": "Existing",
                       "instruction": "Inspect existing state.", "next": "old_done"},
                      "old_done": {"type": "resolution", "title": "Done", "message": "Done."}}}
        self._write(self.root / "app/decision_trees/browser_connectivity.json", canonical)
        claims = [self._spec("CLM-UPDATE", "existing", "instruction", {"title": "Existing",
            "instruction": "Inspect the approved existing state.", "next": "old_done"}, operation="update")]
        self._write_claim_plan(claims)
        package = self.service.prepare(self.campaign_id, self.work_id)
        package = self.service.plan(package["generation_id"])
        self.assertEqual(package["workflow_plan"]["expansion_delta"]["updated"], ["existing"])
        self.assertEqual(package["workflow_plan"]["expansion_delta"]["preserved"], ["old_done"])

    def test_no_research_ai_approval_or_production_mutation_occurs(self):
        campaign_before = deepcopy(json.loads((self.campaign_root / f"{self.campaign_id}.json").read_text()))
        with patch("app.services.knowledge_workflow_generation_service.WorkflowReasoningAuditor.analyze",
                   wraps=self.service.reasoning.analyze) as analyze:
            self._draft()
            analyze.assert_called_once()
        self.assertEqual(json.loads((self.campaign_root / f"{self.campaign_id}.json").read_text()), campaign_before)
        self.assertEqual(list((self.root / "app/workflow_publications").rglob("*.json")), [])

    def test_routes_expose_supervised_plan_draft_review_and_handoff(self):
        with flask_app.test_client() as client, \
             patch("app.app.KnowledgeWorkflowGenerationService", return_value=self.service):
            response = client.post(f"/curator/growth/coverage-campaigns/{self.campaign_id}/workflow-generation",
                                   data={"work_item_id": self.work_id})
            self.assertEqual(response.status_code, 302)
            package = self.service.list_for_campaign(self.campaign_id)[0]
            page = client.get(f"/curator/growth/workflow-generation/{package['generation_id']}")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Create Workflow Plan", page.data)

    def test_planning_is_idempotent(self):
        package = self._planned()
        history_count = len(package["history"])
        repeated = self.service.plan(package["generation_id"])
        self.assertEqual(repeated["workflow_plan"], package["workflow_plan"])
        self.assertEqual(len(repeated["history"]), history_count)

    def test_draft_preparation_is_idempotent(self):
        package = self._draft()
        history_count = len(package["history"])
        repeated = self.service.prepare_draft(package["generation_id"])
        self.assertEqual(repeated["workflow_draft"], package["workflow_draft"])
        self.assertEqual(len(repeated["history"]), history_count)

    def test_question_preserves_diagnostic_evidence_purpose(self):
        question = {item["node_id"]: item for item in self._planned()["workflow_plan"]["nodes"]}["q_start"]
        self.assertIn("wider connectivity failure", question["fields"]["help_text"])
        self.assertEqual(question["claim_ids"], ["CLM-QSTART"])

    def test_all_question_answer_destinations_are_preserved_exactly(self):
        question = {item["node_id"]: item for item in self._planned()["workflow_plan"]["nodes"]}["q_start"]
        self.assertEqual({key: value["next"] for key, value in question["fields"]["answers"].items()},
                         {"yes": "r_done", "no": "i_restart", "unsure": "r_escalate"})

    def test_safe_uncertainty_path_ends_in_evidence_preserving_escalation(self):
        package = self._draft()
        nodes = package["workflow_draft"]["nodes"]
        self.assertEqual(nodes["q_start"]["answers"]["unsure"]["next"], "r_escalate")
        self.assertIn("Record", nodes["r_escalate"]["message"])

    def test_command_syntax_and_shell_metadata_are_not_transformed(self):
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"].update({
            "command": "cmd /c ipconfig /all", "shell": "Command Prompt", "privilege": "standard"
        })
        self._write_claim_plan(claims)
        action = {item["node_id"]: item for item in self._planned()["workflow_plan"]["nodes"]}["i_restart"]
        self.assertEqual(action["fields"]["command"], "cmd /c ipconfig /all")
        self.assertEqual(action["fields"]["shell"], "Command Prompt")
        self.assertEqual(action["fields"]["privilege"], "standard")

    def test_authorization_and_expected_impact_are_preserved(self):
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"].update({
            "authorization": "User approval required", "expected_impact": "Active tabs may reconnect"
        })
        self._write_claim_plan(claims)
        action = {item["node_id"]: item for item in self._planned()["workflow_plan"]["nodes"]}["i_restart"]
        self.assertEqual(action["fields"]["authorization"], "User approval required")
        self.assertEqual(action["fields"]["expected_impact"], "Active tabs may reconnect")

    def test_terminal_claim_language_is_preserved_without_added_certainty(self):
        package = self._draft()
        self.assertEqual(package["workflow_draft"]["nodes"]["r_done"]["message"],
                         "The tested website loads after the browser check.")

    def test_escalation_boundary_is_preserved(self):
        package = self._draft()
        escalation = package["workflow_draft"]["nodes"]["r_escalate"]
        self.assertEqual(escalation["type"], "resolution")
        self.assertIn("before escalating", escalation["message"])

    def test_existing_article_relationship_is_classified_for_reuse(self):
        self._write(self.root / "knowledge_base/published/browser-check.json", {"article_id": "browser-check"})
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"]["knowledge_article"] = "browser-check"
        self._write_claim_plan(claims)
        decisions = self._planned()["workflow_plan"]["reuse_decisions"]
        self.assertIn({"node_id": "i_restart", "asset_type": "article",
                       "asset_id": "browser-check", "decision": "reuse"}, decisions)

    def test_missing_article_relationship_is_visible_before_drafting(self):
        claims = self._claims()
        claims[1]["workflow_spec"]["fields"]["knowledge_article"] = "missing-article"
        self._write_claim_plan(claims)
        decisions = self._planned()["workflow_plan"]["reuse_decisions"]
        self.assertEqual(decisions[0]["decision"], "missing")

    def test_campaign_listing_returns_only_matching_packages(self):
        package = self.service.prepare(self.campaign_id, self.work_id)
        self.assertEqual([item["generation_id"] for item in self.service.list_for_campaign(self.campaign_id)],
                         [package["generation_id"]])
        self.assertEqual(self.service.list_for_campaign("KCP-OTHER"), [])

    def test_unknown_review_decision_is_rejected(self):
        package = self._draft()
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "Unknown review decision"):
            self.service.review(package["generation_id"], "publish")

    def test_approval_requires_a_valid_draft(self):
        package = self.service.prepare(self.campaign_id, self.work_id)
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "valid workflow draft"):
            self.service.review(package["generation_id"], "approved")

    def test_handoff_preserves_package_provenance_in_content_studio_draft(self):
        package = self.service.review(self._draft()["generation_id"], "approved")
        package = self.service.handoff(package["generation_id"])
        stored = json.loads((self.root / "app/workflow_drafts" / package["content_studio_filename"]).read_text())
        provenance = stored["knowledge_factory"]
        self.assertEqual(provenance["generation_id"], package["generation_id"])
        self.assertEqual(provenance["claim_ids"], package["approved_claim_ids"])
        self.assertTrue(provenance["human_reviewed"])

    def test_ineligible_campaign_status_blocks_generation(self):
        self.campaign["status"] = "draft"
        self._write(self.campaign_root / f"{self.campaign_id}.json", self.campaign)
        gate = self.service.eligibility(self.campaign_id, self.work_id)
        self.assertFalse(gate["eligible"])
        self.assertIn("Coverage analysis must be current.", gate["reasons"])

    def test_non_workflow_work_type_blocks_generation(self):
        self.campaign["work_items"][0]["work_type"] = "knowledge_article"
        self._write(self.campaign_root / f"{self.campaign_id}.json", self.campaign)
        gate = self.service.eligibility(self.campaign_id, self.work_id)
        self.assertFalse(gate["eligible"])
        self.assertIn("This work item does not request workflow coverage.", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
