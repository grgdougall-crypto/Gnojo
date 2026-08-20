import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.knowledge.article_schema import create_article_template
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_claim_planning_service import (
    KnowledgeClaimPlanningError,
    KnowledgeClaimPlanningService,
)
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.knowledge_draft_generation_service import KnowledgeDraftGenerationService
from app.services.knowledge_draft_refinement_service import KnowledgeDraftRefinementService
from app.services.knowledge_evidence_extraction_service import CANDIDACY_RULE_VERSION
from app.services.knowledge_workflow_generation_service import KnowledgeWorkflowGenerationService


class KnowledgeClaimPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in (
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
            "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/archive",
            "knowledge_base/commands", "knowledge_base/scripts",
        ):
            (self.root / directory).mkdir(parents=True)
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"schema_version": "1.0", "domains": [{
            "id": "windows-connectivity", "title": "Windows Connectivity",
            "category": "Networking", "platforms": ["Windows"],
            "areas": [{"id": "vpn", "title": "VPN", "terms": ["vpn", "connectivity"]}],
        }]}), encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": "1.0", "tiers": [{
            "tier": 1, "label": "First-party", "publishers": [{
                "name": "Microsoft", "domains": ["learn.microsoft.com"],
            }],
        }], "research_targets": []}), encoding="utf-8")
        self.campaign_root = self.root / "campaigns"
        planner = KnowledgeCoveragePlannerService(self.root, self.campaign_root, self.taxonomy)
        campaign = planner.analyze(planner.create(
            title="Connectivity", domain_id="windows-connectivity", objective="Improve guidance."
        )["campaign_id"])
        self.gap = next(item for item in campaign["gaps"] if item["gap_type"] == "missing_article")
        self.work = next(item for item in campaign["work_items"] if item["gap_id"] == self.gap["gap_id"])
        self.repository = KnowledgeRepository(self.root / "knowledge_base")
        self.generation = KnowledgeDraftGenerationService(
            self.root, self.campaign_root, self.taxonomy, self.policy, self.repository
        )
        self.research_id = "KRP-AAAAAAAAAAAA"
        self._write_research()
        self.package = self.generation.prepare(
            self.work["campaign_id"], self.gap["gap_id"], self.work["work_item_id"]
        )
        self.service = KnowledgeClaimPlanningService(self.generation, self.campaign_root)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_research(self):
        root = self.campaign_root / "research"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{self.research_id}.json").write_text(json.dumps({
            "schema_version": "1.0", "package_id": self.research_id,
            "campaign_id": self.work["campaign_id"], "gap_id": "KCG-SOURCE",
            "work_item_id": "KCW-SOURCE", "target_coverage_area": "vpn",
            "status": "approved", "created_at": "now", "selected_sources": ["KSC-MICROSOFT"],
            "candidate_sources": [{"source_candidate_id": "KSC-MICROSOFT",
                "review_state": "selected", "topic_relevant": True, "authority_tier": 1,
                "publisher": "Microsoft", "canonical_url": "https://learn.microsoft.com/vpn",
                "page_title": "VPN guide", "applicable_platform": "Windows",
                "provenance": {"content_digest": "source-digest"}}],
        }), encoding="utf-8")

    def _write_evidence(self, units=None, status=None):
        units = units or [
            self._unit("EVD-PROCEDURE01", "procedure", "Open Settings and select Network and Internet."),
            self._unit("EVD-VERIFY00001", "verification", "Verify the VPN status displays Connected."),
        ]
        root = self.campaign_root / "evidence_extraction"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "KEX-AAAAAAAAAAAA.json"
        for unit in units:
            unit["candidacy"] = {
                "machine_recommended_role": "candidate",
                "machine_rationale": "Deterministic test fixture evidence.",
                "rule_version": CANDIDACY_RULE_VERSION,
                "recommendation_fingerprint": f"recommend-{unit['evidence_id']}",
                "human_confirmed_role": "candidate",
                "role_decided_at": "now",
                "role_decided_by": "Human",
            }
        if status is None:
            states = [unit.get("review_state", "proposed") for unit in units]
            status = ("approved" if states and "proposed" not in states and "approved" in states
                      else "partially_approved" if "approved" in states else "needs_review")
        package = {"schema_version": "1.0", "extraction_id": "KEX-AAAAAAAAAAAA",
            "research_package_id": self.research_id, "status": status, "created_at": "now",
            "evidence_units": units, "source_fingerprint": "source-digest", "revision": 1,
            "campaign_id": self.work["campaign_id"], "gap_id": self.work["gap_id"],
            "work_item_id": self.work["work_item_id"], "platform": "Windows",
            "candidacy": {"schema_version": "1.0", "rule_version": CANDIDACY_RULE_VERSION,
                           "candidate_set_status": "confirmed", "confirmed_at": "now",
                           "confirmed_by": "Human", "confirmation_fingerprint": None}}
        package["candidacy"]["confirmation_fingerprint"] = \
            self.generation.extraction._candidate_set_fingerprint(package)
        path.write_text(json.dumps(package), encoding="utf-8")
        return path

    def _unit(self, evidence_id, evidence_type, text, review_state="approved", **extra):
        value = {"evidence_id": evidence_id, "evidence_type": evidence_type,
            "normalized_claim": text, "review_state": review_state,
            "source_title": "VPN guide", "source_url": "https://learn.microsoft.com/vpn",
            "publisher": "Microsoft", "fingerprint": f"fp-{evidence_id}", "confidence": "high",
            "platform_applicability": "Windows", "provenance": {
                "research_package_id": self.research_id, "source_candidate_id": "KSC-MICROSOFT",
                "extraction_id": "KEX-AAAAAAAAAAAA"},
        }
        value.update(extra)
        return value

    def _planned(self, units=None):
        self._write_evidence(units)
        plan = self.service.prepare(self.package["package_id"])
        return self.service.plan(plan["claim_plan_id"])

    def _approve_all(self, plan):
        for claim in plan["claims"]:
            plan = self.service.review_claim(plan["claim_plan_id"], claim["claim_id"], "approved")
        for section in plan["sections"]:
            if section["claim_ids"]:
                plan = self.service.review_section(plan["claim_plan_id"], section["section"], "approved")
        return plan

    def _workflow_plan(self, *, evidence_status="approved", unit_states=None):
        campaign_path = self.campaign_root / f"{self.work['campaign_id']}.json"
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        gap = {"gap_id": "KCG-WORKFLOW", "gap_type": "missing_safety",
               "area_id": "vpn", "title": "VPN workflow safety"}
        work = {"campaign_id": self.work["campaign_id"], "gap_id": gap["gap_id"],
                "work_item_id": "KCW-WORKFLOW", "work_type": "safety_review",
                "area_id": "vpn", "target_asset": "vpn_diagnostics", "status": "proposed",
                "dependencies": [], "reason": "Add governed VPN diagnostic safety."}
        campaign.setdefault("gaps", []).append(gap)
        campaign.setdefault("work_items", []).append(work)
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")

        research = json.loads((self.campaign_root / "research" / f"{self.research_id}.json").read_text())
        research.update({"work_item_id": work["work_item_id"], "gap_id": gap["gap_id"]})
        (self.campaign_root / "research" / f"{self.research_id}.json").write_text(
            json.dumps(research), encoding="utf-8")
        states = unit_states or {}
        units = [
            self._unit("EVD-WF-PROCEDURE", "procedure", "Open the VPN status page and record its state.",
                       states.get("procedure", "approved")),
            self._unit("EVD-WF-SAFETY", "safety", "Do not disconnect an active remote support session.",
                       states.get("safety", "approved")),
            self._unit("EVD-WF-AUTH", "authorization_requirements", "Obtain authorization before changing VPN settings.",
                       states.get("authorization", "approved")),
            self._unit("EVD-WF-VERIFY", "verification", "Verify that the VPN status displays Connected.",
                       states.get("verification", "approved")),
        ]
        self._write_evidence(units, evidence_status)
        plan = self.service.prepare_workflow(self.work["campaign_id"], work["work_item_id"])
        return self.service.plan(plan["claim_plan_id"]), work

    def test_human_initiation_and_approved_evidence_are_required(self):
        self.assertEqual(self.service.list_for_kdg(self.package["package_id"]), [])
        with self.assertRaisesRegex(KnowledgeClaimPlanningError, "Approved Phase 5 evidence"):
            self.service.prepare(self.package["package_id"])
        self._write_evidence([self._unit("EVD-UNAPPROVED", "procedure", "Open Settings.", "proposed")])
        with self.assertRaisesRegex(KnowledgeClaimPlanningError, "Approved Phase 5 evidence"):
            self.service.prepare(self.package["package_id"])

    def test_stable_plan_claim_identity_and_idempotent_replanning(self):
        first = self._planned()
        second = self.service.plan(first["claim_plan_id"])
        self.assertRegex(first["claim_plan_id"], r"^KCPM-[A-F0-9]{12}$")
        self.assertTrue(all(item["claim_id"].startswith("CLM-") for item in first["claims"]))
        self.assertEqual(first, second)

    def test_section_mapping_claim_types_support_and_gaps(self):
        plan = self._planned()
        claims = {item["section"]: item for item in plan["claims"]}
        self.assertEqual(claims["procedure"]["claim_type"], "action_procedure")
        self.assertEqual(claims["verification"]["claim_type"], "verification")
        self.assertEqual(claims["procedure"]["support_level"], "direct")
        self.assertFalse(plan["evidence_gaps"])
        sections = {item["section"]: item for item in plan["sections"]}
        self.assertTrue(sections["purpose"]["applicable"])
        self.assertFalse(sections["commands"]["applicable"])
        missing = self._planned([self._unit("EVD-PROCEDURE01", "procedure", "Open Settings.")])
        self.assertEqual([item["section"] for item in missing["evidence_gaps"]], ["verification"])
        self.assertEqual(missing["status"], "needs_evidence")

    def test_multi_evidence_corroboration_deduplicates_claim(self):
        text = "Verify the VPN status displays Connected."
        plan = self._planned([
            self._unit("EVD-VERIFY00001", "verification", text),
            self._unit("EVD-VERIFY00002", "verification", text,
                       source_url="https://learn.microsoft.com/vpn-2"),
            self._unit("EVD-PROCEDURE01", "procedure", "Open Settings."),
        ])
        matches = [item for item in plan["claims"] if item["normalized_claim"] == text]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["support_level"], "corroborated")
        self.assertEqual(len(matches[0]["evidence_ids"]), 2)

    def test_conditional_partial_and_conflicting_support(self):
        plan = self._planned([
            self._unit("EVD-CONDITIONAL1", "procedure", "If authorized, restart the VPN client."),
            self._unit("EVD-PARTIAL0001", "verification", "The status is displayed.", confidence="low"),
            self._unit("EVD-CONFLICT001", "preconditions", "This action requires administrator access."),
            self._unit("EVD-CONFLICT002", "preconditions", "This action does not require administrator access."),
        ])
        support = {item["normalized_claim"]: item["support_level"] for item in plan["claims"]}
        self.assertEqual(support["If authorized, restart the VPN client."], "conditional")
        self.assertEqual(support["The status is displayed."], "partial")
        self.assertTrue(plan["conflicts"])
        self.assertEqual(plan["status"], "needs_conflict_resolution")

    def test_human_review_states_and_rejected_claim_exclusion(self):
        plan = self._planned()
        first, second = plan["claims"]
        plan = self.service.review_claim(plan["claim_plan_id"], first["claim_id"], "approved", "Checked")
        self.assertEqual(plan["status"], "partially_approved")
        plan = self.service.review_claim(plan["claim_plan_id"], second["claim_id"], "needs_revision")
        self.assertEqual(self.service.approved_claims_for(self.package["package_id"]), [])
        plan = self.service.review_claim(plan["claim_plan_id"], second["claim_id"], "rejected")
        self.assertNotIn(second["claim_id"], [item["claim_id"] for item in
                                              self.service.approved_claims_for(self.package["package_id"])])

    def test_section_mapping_requires_explicit_human_review(self):
        plan = self._planned()
        for claim in plan["claims"]:
            plan = self.service.review_claim(plan["claim_plan_id"], claim["claim_id"], "approved")
        self.assertNotEqual(plan["status"], "ready_for_drafting")
        mapped = next(item for item in plan["sections"] if item["claim_ids"])
        reviewed = self.service.review_section(plan["claim_plan_id"], mapped["section"], "needs_revision",
                                               "Mapping needs review")
        state = next(item for item in reviewed["sections"] if item["section"] == mapped["section"])
        self.assertEqual(state["review_state"], "needs_revision")

    def test_conflict_decision_survives_changed_evidence_replan(self):
        units = [
            self._unit("EVD-CONFLICT001", "preconditions", "This action requires administrator access."),
            self._unit("EVD-CONFLICT002", "preconditions", "This action does not require administrator access."),
            self._unit("EVD-PROCEDURE01", "procedure", "Open Settings."),
            self._unit("EVD-VERIFY00001", "verification", "Verify Connected is displayed."),
        ]
        plan = self._planned(units)
        conflict = plan["conflicts"][0]
        self.service.review_conflict(plan["claim_plan_id"], conflict["conflict_id"], "scoped", "Windows only")
        units.append(self._unit("EVD-SYMPTOM0001", "symptoms", "The VPN cannot connect."))
        self._write_evidence(units)
        changed = self.service.plan(plan["claim_plan_id"])
        self.assertEqual(changed["conflicts"][0]["resolution"], "scoped")
        self.assertTrue(changed["revisions"])

    def test_stale_evidence_is_excluded_downstream(self):
        plan = self._approve_all(self._planned())
        self.assertEqual(plan["status"], "ready_for_drafting")
        self.assertTrue(self.service.approved_claims_for(self.package["package_id"]))
        path = self.campaign_root / "evidence_extraction" / "KEX-AAAAAAAAAAAA.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = "needs_refresh"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.service.approved_claims_for(self.package["package_id"]), [])
        current = self.service.get(plan["claim_plan_id"])
        self.assertEqual(current["status"], "needs_evidence")
        self.assertTrue(current["validation"]["stale_evidence_ids"])
        self.assertTrue(any(claim["stale"] for claim in current["claims"]))

    def test_deferred_conflict_remains_blocking(self):
        units = [
            self._unit("EVD-CONFLICT001", "preconditions", "This action requires administrator access."),
            self._unit("EVD-CONFLICT002", "preconditions", "This action does not require administrator access."),
            self._unit("EVD-PROCEDURE01", "procedure", "Open Settings."),
            self._unit("EVD-VERIFY00001", "verification", "Verify Connected is displayed."),
        ]
        plan = self._planned(units)
        conflict = plan["conflicts"][0]
        reviewed = self.service.review_conflict(
            plan["claim_plan_id"], conflict["conflict_id"], "deferred", "Needs human follow-up"
        )
        self.assertEqual(reviewed["status"], "needs_conflict_resolution")

    def test_canonical_reuse_candidate_is_traceable(self):
        article = create_article_template()
        article.update({"id": "shared-vpn-verification", "canonical_id": "shared-vpn-verification",
            "title": "Shared VPN Verification", "category": "Networking", "overview": "Reusable.",
            "sources": [{"title": "Official", "url": "https://learn.microsoft.com/vpn"}],
            "generation": {"provider": "Human", "model": "manual", "generated_at": "now"},
            "review": {"status": "approved", "reviewed_by": "Human", "reviewed_at": "now", "notes": []}})
        self.repository.save_published(article)
        package = self.generation.get(self.package["package_id"])
        package["existing_assets_considered"].append({"content_type": "article",
            "identifier": article["id"], "title": article["title"], "state": "published"})
        self.generation._save(package)
        plan = self._planned()
        self.assertEqual(plan["canonical_reuse"][0]["article_id"], article["id"])
        self.assertTrue(plan["canonical_reuse"][0]["traceable"])

    def test_phase_three_and_four_consume_only_approved_claims(self):
        plan = self._planned()
        procedure = next(item for item in plan["claims"] if item["section"] == "procedure")
        verification = next(item for item in plan["claims"] if item["section"] == "verification")
        self.service.review_claim(plan["claim_plan_id"], procedure["claim_id"], "approved")
        partial = self.generation.refresh_from_approved_claim_plan(self.package["package_id"])
        self.assertEqual(partial["generation_status"], "needs_evidence")
        self.assertIsNone(partial["draft_preview"])
        plan = self.service.review_claim(plan["claim_plan_id"], verification["claim_id"], "approved")
        for section in plan["sections"]:
            if section["claim_ids"]:
                plan = self.service.review_section(plan["claim_plan_id"], section["section"], "approved")
        ready = self.generation.refresh_from_approved_claim_plan(self.package["package_id"])
        self.assertEqual(ready["draft_preview"]["knowledge_factory"]["claim_plan_id"], plan["claim_plan_id"])
        refined = KnowledgeDraftRefinementService(self.generation).refine(self.package["package_id"])
        planned = [item for item in refined["refinement"]["evidence_records"]
                   if item["kind"] == "approved_claim_plan"]
        self.assertEqual({item["evidence_id"] for item in planned},
                         {procedure["claim_id"], verification["claim_id"]})

    def test_planning_does_not_publish_research_or_mutate_protected_systems(self):
        protected = []
        for name in ("app/decision_trees/sentinel.json", "curation_runs/latest.json",
                     "curation_memory/reasoning_calibration.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8")
            protected.append(path)
        before = {path: path.read_bytes() for path in protected}
        self._planned()
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        self.assertEqual(self.repository.get_published(), [])
        self.assertEqual(self.repository.get_drafts(), [])

    def test_no_network_or_llm_routing_was_added(self):
        source = Path("app/services/knowledge_claim_planning_service.py").read_text(encoding="utf-8").casefold()
        for forbidden in ("requests.", "urlopen", "openai", "gemini"):
            self.assertNotIn(forbidden, source)

    def test_ui_requires_human_prepare_and_exposes_supervised_review(self):
        self._write_evidence()
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeDraftGenerationService", return_value=self.generation), \
             patch("app.app.KnowledgeClaimPlanningService", return_value=self.service):
            with flask_app.test_client() as client:
                detail = client.get(
                    f"/curator/growth/draft-generation/{self.package['package_id']}"
                )
                self.assertEqual(detail.status_code, 200)
                self.assertIn(b"Plan Article Claims", detail.data)
                self.assertEqual(self.service.list_for_kdg(self.package["package_id"]), [])
                prepared = client.post(
                    f"/curator/growth/draft-generation/{self.package['package_id']}/claim-planning"
                )
                self.assertEqual(prepared.status_code, 302)
                plan = self.service.list_for_kdg(self.package["package_id"])[0]
                page = client.get(f"/curator/growth/claim-planning/{plan['claim_plan_id']}")
                self.assertIn(b"Planned claims are not approved claims", page.data)
                self.assertIn(b"Build Evidence Plan", page.data)
                self.assertNotIn(b"Publish article", page.data)

    def test_workflow_claim_plan_reuses_kcpm_and_exact_phase_eight_contract(self):
        plan, _ = self._workflow_plan()
        repeated = self.service.plan(plan["claim_plan_id"])
        self.assertEqual(plan, repeated)
        self.assertEqual(plan["target_asset_type"], "workflow")
        self.assertRegex(plan["claim_plan_id"], r"^KCPM-[A-F0-9]{12}$")
        self.assertEqual(len(plan["history"]), len(repeated["history"]))
        self.assertEqual(len({claim["claim_id"] for claim in plan["claims"]}), len(plan["claims"]))
        for claim in plan["claims"]:
            self.assertTrue(claim["claim_id"].startswith("CLM-"))
            self.assertTrue(claim["evidence_ids"])
            self.assertEqual(claim["source_urls"], ["https://learn.microsoft.com/vpn"])
            spec = claim["workflow_spec"]
            self.assertIn(spec["type"], {"question", "instruction", "resolution", "transition"})
            self.assertIsInstance(spec["fields"], dict)
        self.assertFalse(any(claim["workflow_spec"]["type"] == "question" for claim in plan["claims"]))
        terminal = [claim for claim in plan["claims"]
                    if claim["workflow_spec"]["type"] == "resolution"]
        self.assertEqual(terminal[0]["workflow_spec"]["fields"]["message"],
                         "Verify that the VPN status displays Connected.")

    def test_workflow_planning_requires_current_approved_evidence(self):
        with self.assertRaisesRegex(KnowledgeClaimPlanningError, "Approved Phase 5 evidence"):
            self._workflow_plan(unit_states={"procedure": "proposed", "safety": "proposed",
                                             "authorization": "proposed", "verification": "proposed"})

    def test_workflow_safety_authorization_and_verification_remain_supervised(self):
        plan, work = self._workflow_plan()
        by_type = {claim["claim_type"]: claim for claim in plan["claims"]}
        self.assertIn("caution", by_type)
        self.assertIn("authorization_requirement", by_type)
        self.assertIn("verification", by_type)
        self.assertTrue(all(claim["review_state"] == "proposed" for claim in plan["claims"]))
        phase_eight = KnowledgeWorkflowGenerationService(
            self.root, self.campaign_root, self.root / "app" / "workflow_drafts")
        self.assertFalse(phase_eight.eligibility(self.work["campaign_id"], work["work_item_id"])["eligible"])
        revision = self.service.review_claim(plan["claim_plan_id"], plan["claims"][0]["claim_id"],
                                             "needs_revision")
        self.assertNotEqual(revision["status"], "ready_for_drafting")
        rejected = self.service.review_claim(plan["claim_plan_id"], plan["claims"][0]["claim_id"],
                                             "rejected")
        self.assertNotEqual(rejected["status"], "ready_for_drafting")
        approved = self._approve_all(self.service.plan(plan["claim_plan_id"]))
        self.assertEqual(approved["status"], "ready_for_drafting")
        self.assertTrue(phase_eight.eligibility(
            self.work["campaign_id"], work["work_item_id"])["eligible"])
        self.assertEqual(list((self.campaign_root / "workflow_generation").glob("KWG-*.json")), [])

    def test_stale_workflow_evidence_revokes_phase_eight_eligibility(self):
        plan, work = self._workflow_plan()
        self._approve_all(plan)
        evidence_path = self.campaign_root / "evidence_extraction" / "KEX-AAAAAAAAAAAA.json"
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
        value["status"] = "needs_refresh"
        evidence_path.write_text(json.dumps(value), encoding="utf-8")
        current = self.service.get(plan["claim_plan_id"])
        self.assertEqual(current["status"], "needs_evidence")
        phase_eight = KnowledgeWorkflowGenerationService(
            self.root, self.campaign_root, self.root / "app" / "workflow_drafts")
        self.assertFalse(phase_eight.eligibility(
            self.work["campaign_id"], work["work_item_id"])["eligible"])

    def test_workflow_claim_planning_has_a_human_initiated_campaign_route(self):
        plan, work = self._workflow_plan()
        flask_app.config.update(TESTING=True)
        with patch("app.app.KnowledgeClaimPlanningService", return_value=self.service):
            with flask_app.test_client() as client:
                response = client.post(
                    f"/curator/growth/coverage-campaigns/{self.work['campaign_id']}"
                    f"/work-items/{work['work_item_id']}/workflow-claim-planning"
                )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/curator/growth/claim-planning/{plan['claim_plan_id']}",
                      response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
