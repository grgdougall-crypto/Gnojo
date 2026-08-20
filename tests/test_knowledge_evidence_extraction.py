import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app

from app.services.knowledge_evidence_extraction_service import (
    KnowledgeEvidenceExtractionError,
    KnowledgeEvidenceExtractionService,
)


class FakeValidator:
    def __init__(self, html=None, digest="digest-1", final_url="https://learn.microsoft.com/windows/vpn"):
        self.html = html or """<html><head><title>VPN guide</title></head><body><main>
        <h2>Before you begin</h2><p>Before you begin, sign in with an administrator account.</p>
        <h2>Resolve the problem</h2><ol><li>Open Settings and select Network and Internet.</li>
        <li>Confirm that the VPN connection displays Connected.</li></ol>
        <p>If this does not resolve the issue, contact support.</p>
        </main><footer>Advertising and cookie controls</footer></body></html>"""
        self.digest, self.final_url = digest, final_url
        self.fail = None

    def inspect(self, url):
        if self.fail:
            raise self.fail
        return {"http_status": 200, "final_url": self.final_url, "redirect_chain": [],
                "page_title": "VPN guide", "content_type": "text/html",
                "last_modified": "today", "etag": "v1", "content_digest": self.digest,
                "content_preview": self.html}


class KnowledgeEvidenceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign_root = self.root / "campaigns"
        (self.campaign_root / "research").mkdir(parents=True)
        (self.root / "knowledge_base").mkdir()
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({
            "schema_version": "1.0", "tiers": [{"tier": 1, "label": "First-party",
            "publishers": [{"name": "Microsoft", "domains": ["learn.microsoft.com"]}]}],
            "research_targets": [],
        }), encoding="utf-8")
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"schema_version": "1.0", "domains": []}), encoding="utf-8")
        self.research = {
            "schema_version": "1.0", "package_id": "KRP-AAAAAAAAAAAA",
            "campaign_id": "KCP-AAAAAAAAAAAA", "gap_id": "KCG-AAAAAAAAAAAA",
            "work_item_id": "KCW-AAAAAAAAAAAA", "status": "approved",
            "platform": "Windows", "product_vendor": "Microsoft", "created_at": "now",
            "selected_sources": ["KSC-AAAAAAAAAAAA"], "candidate_sources": [{
                "source_candidate_id": "KSC-AAAAAAAAAAAA", "review_state": "selected",
                "topic_relevant": True, "authority_tier": 1, "publisher": "Microsoft",
                "canonical_url": "https://learn.microsoft.com/windows/vpn",
                "page_title": "VPN guide", "applicable_platform": "Windows",
                "provenance": {"content_digest": "research-digest"},
            }],
        }
        self.research_path = self.campaign_root / "research" / "KRP-AAAAAAAAAAAA.json"
        self.research_path.write_text(json.dumps(self.research), encoding="utf-8")
        (self.campaign_root / "KCP-AAAAAAAAAAAA.json").write_text(json.dumps({
            "schema_version": "1.0", "campaign_id": "KCP-AAAAAAAAAAAA",
            "title": "Windows Connectivity Production Coverage Review",
            "scope": "Windows Connectivity", "platforms": ["Windows"],
            "category": "Networking", "domain": "windows-connectivity",
            "objective": "Improve governed Windows connectivity coverage.",
            "status": "in_progress", "created_at": "now",
            "gaps": [{"gap_id": "KCG-AAAAAAAAAAAA", "gap_type": "missing_safety",
                      "summary": "Review DNS diagnostic safety evidence."}],
            "work_items": [{"work_item_id": "KCW-AAAAAAAAAAAA",
                            "gap_id": "KCG-AAAAAAAAAAAA", "area_id": "DNS",
                            "work_type": "safety_review", "status": "open"}],
        }), encoding="utf-8")
        self.validator = FakeValidator()
        self.service = KnowledgeEvidenceExtractionService(
            self.root, self.campaign_root, self.policy, self.taxonomy, self.validator,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        return self.service.prepare("KRP-AAAAAAAAAAAA", "KSC-AAAAAAAAAAAA")

    def confirm_all_candidates(self, extraction_id):
        package = self.service.get(extraction_id)
        for unit in package.get("evidence_units", []):
            self.service.set_candidacy_role(extraction_id, unit["evidence_id"], "candidate")
        return self.service.confirm_candidate_set(extraction_id)

    def review_evidence(self, extraction_id, evidence_id, decision, notes=""):
        if not self.service._candidate_set_current(self.service.get(extraction_id)):
            self.confirm_all_candidates(extraction_id)
        return self.service.review_evidence(extraction_id, evidence_id, decision, notes)

    def test_requires_approved_selected_authoritative_source(self):
        value = dict(self.research)
        value["status"] = "ready_for_review"
        self.research_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(KnowledgeEvidenceExtractionError, "approved Phase 2"):
            self.prepare()

    def test_prepare_has_stable_kex_identity_and_is_idempotent(self):
        first, second = self.prepare(), self.prepare()
        self.assertRegex(first["extraction_id"], r"^KEX-[A-F0-9]{12}$")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "proposed")

    def test_extraction_records_retrieval_and_bounded_traceable_units(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertEqual(package["status"], "needs_review")
        self.assertEqual(package["retrieval"]["requested_url"], self.research["candidate_sources"][0]["canonical_url"])
        self.assertEqual(package["retrieval"]["http_status"], 200)
        self.assertTrue(package["evidence_units"])
        self.assertTrue(all(len(unit["supporting_passage"]) <= self.service.MAX_PASSAGE
                            for unit in package["evidence_units"]))
        self.assertTrue(all(unit["evidence_id"].startswith("EVD-") for unit in package["evidence_units"]))
        self.assertTrue(all(unit["provenance"]["source_candidate_id"] == "KSC-AAAAAAAAAAAA"
                            for unit in package["evidence_units"]))
        self.assertNotIn("Advertising", json.dumps(package["evidence_units"]))

    def test_supervised_candidacy_is_orthogonal_confirmed_and_auditable(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first, second = package["evidence_units"][:2]
        original_ids = [unit["evidence_id"] for unit in package["evidence_units"]]
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in package["evidence_units"]))
        self.service.set_candidacy_role(package["extraction_id"], first["evidence_id"], "candidate")
        self.service.set_candidacy_role(package["extraction_id"], second["evidence_id"], "context")
        with self.assertRaisesRegex(KnowledgeEvidenceExtractionError, "Assign every"):
            self.service.confirm_candidate_set(package["extraction_id"])
        for unit in package["evidence_units"][2:]:
            self.service.set_candidacy_role(package["extraction_id"], unit["evidence_id"], "context")
        confirmed = self.service.confirm_candidate_set(package["extraction_id"])
        repeated = self.service.confirm_candidate_set(package["extraction_id"])
        self.assertEqual(confirmed["history"], repeated["history"])
        self.assertEqual(original_ids, [unit["evidence_id"] for unit in confirmed["evidence_units"]])
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in confirmed["evidence_units"]))
        self.assertIn("candidate_set_confirmed", [event["event"] for event in confirmed["history"]])
        with self.assertRaisesRegex(KnowledgeEvidenceExtractionError, "Candidate Evidence"):
            self.service.review_evidence(package["extraction_id"], second["evidence_id"], "approved")

    def test_context_does_not_block_completion_or_enter_phase_six(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first = package["evidence_units"][0]
        for unit in package["evidence_units"]:
            self.service.set_candidacy_role(package["extraction_id"], unit["evidence_id"],
                                            "candidate" if unit is first else "context")
        self.service.confirm_candidate_set(package["extraction_id"])
        self.service.review_evidence(package["extraction_id"], first["evidence_id"], "approved")
        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertTrue(workspace["complete"])
        self.assertEqual(workspace["counts"]["total"], 1)
        self.assertEqual([u["evidence_id"] for u in self.service.approved_units_for(
            ["KRP-AAAAAAAAAAAA"])], [first["evidence_id"]])

    def test_empty_candidate_set_can_be_human_confirmed_without_approving_evidence(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            self.service.set_candidacy_role(
                package["extraction_id"], unit["evidence_id"], "context")

        ready = self.service.review_workspace(package["extraction_id"])
        self.assertEqual(ready["unresolved_candidacy"], 0)
        self.assertEqual(ready["candidate_count"], 0)
        self.assertTrue(ready["candidate_set_empty"])
        self.assertTrue(ready["candidacy_ready_to_confirm"])

        confirmed = self.service.confirm_candidate_set(package["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertEqual(confirmed["status"], "insufficient_evidence")
        self.assertEqual(confirmed["candidacy"]["candidate_set_outcome"], "empty")
        self.assertTrue(workspace["candidate_set_current"])
        self.assertFalse(workspace["complete"])
        self.assertTrue(all(unit["review_state"] == "proposed"
                            for unit in confirmed["evidence_units"]))
        self.assertTrue(all(unit["candidacy"]["human_confirmed_role"] == "context"
                            for unit in confirmed["evidence_units"]))
        event = confirmed["history"][-1]
        self.assertEqual(event["event"], "candidate_set_confirmed")
        self.assertEqual(event["candidate_set_outcome"], "empty")
        self.assertEqual(event["candidate_count"], 0)
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_empty_candidate_set_confirmation_is_explicit_in_detail_ui(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            self.service.set_candidacy_role(
                package["extraction_id"], unit["evidence_id"], "context")
        workspace = self.service.review_workspace(package["extraction_id"])
        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            )
        html = response.get_data(as_text=True)
        self.assertIn("Candidate set is empty", html)
        self.assertIn("Confirm Empty Candidate Set", html)
        self.assertIn("approves no evidence", html)

    def test_all_suppressed_extraction_still_renders_empty_candidate_confirmation(self):
        """An extraction with source material but no reviewable units is review-complete."""
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            unit["content_disposition"] = {
                "status": "suppressed_non_substantive",
                "reason_code": "structural_non_substantive",
                "explanation": "Deterministically suppressed test fixture.",
                "rule_version": "test",
            }
        self.service._save(package)

        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertGreater(workspace["suppressed_count"], 0)
        self.assertEqual(workspace["reviewable_count"], 0)
        self.assertEqual(workspace["counts"]["total"], 0)
        self.assertEqual(workspace["unresolved_candidacy"], 0)
        self.assertEqual(workspace["candidate_count"], 0)
        self.assertTrue(workspace["candidate_set_empty"])
        self.assertTrue(workspace["candidacy_ready_to_confirm"])

        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("0 of 0 reviewed", html)
        self.assertIn("0 unresolved", html)
        self.assertIn("Candidate set is empty", html)
        self.assertIn("Confirm Empty Candidate Set", html)
        self.assertNotIn("Assign all Machine Context as Reviewer Context", html)

    def test_twenty_three_unit_candidate_set_preserves_context_and_gates_phase_six(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        seed = package["evidence_units"][0]
        units = []
        for index in range(23):
            unit = deepcopy(seed)
            unit["evidence_id"] = f"EVD-{index:012d}"
            unit["fingerprint"] = f"fingerprint-{index}"
            unit["normalized_claim"] = f"Representative evidence statement {index}."
            unit["review_state"] = "approved" if index == 0 else "proposed"
            unit["candidacy"] = self.service.candidacy_recommendation(
                unit, self.service._governed_context(package))
            units.append(unit)
        package["evidence_units"] = units
        package["status"] = "needs_review"
        package["candidacy"] = self.service._empty_candidacy_state()
        self.service._save(package)
        for index, unit in enumerate(units):
            self.service.set_candidacy_role(
                package["extraction_id"], unit["evidence_id"],
                "candidate" if index < 4 else "context")
        confirmed = self.service.confirm_candidate_set(package["extraction_id"])
        self.assertEqual(len(confirmed["evidence_units"]), 23)
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])
        for unit in confirmed["evidence_units"][1:4]:
            self.service.review_evidence(package["extraction_id"], unit["evidence_id"], "rejected")
        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertTrue(workspace["complete"])
        self.assertEqual(workspace["counts"]["total"], 4)
        self.assertEqual(len([unit for unit in workspace["package"]["evidence_units"]
                              if unit["candidacy"]["human_confirmed_role"] == "context"]), 19)
        self.assertEqual([unit["evidence_id"] for unit in self.service.approved_units_for(
            ["KRP-AAAAAAAAAAAA"])], [units[0]["evidence_id"]])

    def test_machine_and_human_role_filters_are_independent_read_only_dimensions(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        seed = package["evidence_units"][0]
        units = []
        for index in range(23):
            unit = deepcopy(seed)
            unit["evidence_id"] = f"EVD-{index:012d}"
            unit["fingerprint"] = f"filter-fingerprint-{index}"
            unit["normalized_claim"] = f"Filter evidence statement {index}."
            unit["review_state"] = "proposed"
            unit["candidacy"] = {
                "machine_recommended_role": "candidate" if index < 9 else "context",
                "machine_rationale": "Deterministic test recommendation.",
                "human_confirmed_role": None,
                "human_confirmed_at": None,
            }
            units.append(unit)
        package["evidence_units"] = units
        package["status"] = "needs_review"
        package["candidacy"] = self.service._empty_candidacy_state()
        self.service._save(package)
        package_path = self.service._path(package["extraction_id"])
        before = package_path.read_bytes()

        all_units = self.service.review_workspace(package["extraction_id"])
        candidates = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="candidate")
        contexts = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="context")
        unresolved_candidates = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="candidate",
            human_role="unresolved")
        human_candidates = self.service.review_workspace(
            package["extraction_id"], human_role="candidate")

        self.assertEqual(all_units["machine_recommendation_counts"], {
            "candidate": 9, "context": 14, "undetermined": 0,
        })
        self.assertEqual(all_units["human_role_counts"], {
            "candidate": 0, "context": 0, "unresolved": 23,
        })
        self.assertEqual(len(candidates["units"]), 9)
        self.assertEqual(len(contexts["units"]), 14)
        self.assertEqual(len(unresolved_candidates["units"]), 9)
        self.assertEqual(human_candidates["units"], [])
        self.assertTrue(all(unit["candidacy_role"] == "unresolved"
                            for unit in unresolved_candidates["units"]))
        self.assertFalse(all_units["candidate_set_current"])
        self.assertEqual(package_path.read_bytes(), before)

    def test_invalid_split_filter_values_normalize_without_mutating_package(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        package_path = self.service._path(package["extraction_id"])
        before = package_path.read_bytes()
        workspace = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="automatic-accept",
            human_role="https://evil.example")
        self.assertEqual(workspace["machine_recommendation"], "all")
        self.assertEqual(workspace["human_role"], "all")
        self.assertEqual(package_path.read_bytes(), before)

    def test_governed_context_change_stales_confirmation_without_losing_decisions(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            self.service.set_candidacy_role(package["extraction_id"], unit["evidence_id"], "candidate")
        confirmed = self.service.confirm_candidate_set(package["extraction_id"])
        first = confirmed["evidence_units"][0]
        self.service.review_evidence(package["extraction_id"], first["evidence_id"], "approved")
        campaign_path = self.campaign_root / f"{package['campaign_id']}.json"
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign["objective"] = "A materially revised governed objective."
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertFalse(workspace["candidate_set_current"])
        preserved = next(unit for unit in workspace["package"]["evidence_units"]
                         if unit["evidence_id"] == first["evidence_id"])
        self.assertEqual(preserved["review_state"], "approved")
        self.assertEqual(preserved["candidacy"]["human_confirmed_role"], "candidate")
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_extraction_never_approves_and_downstream_sees_only_human_approved(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in package["evidence_units"]))
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])
        target = package["evidence_units"][0]
        reviewed = self.review_evidence(package["extraction_id"], target["evidence_id"], "approved", "Checked")
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])
        self.assertEqual(reviewed["status"], "partially_approved")
        for unit in package["evidence_units"][1:]:
            self.service.review_evidence(package["extraction_id"], unit["evidence_id"], "rejected")
        approved = self.service.approved_units_for(["KRP-AAAAAAAAAAAA"])
        self.assertEqual([unit["evidence_id"] for unit in approved], [target["evidence_id"]])

    def test_rejected_and_needs_revision_units_are_unavailable(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first, second = package["evidence_units"][:2]
        self.review_evidence(package["extraction_id"], first["evidence_id"], "rejected")
        self.review_evidence(package["extraction_id"], second["evidence_id"], "needs_revision")
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_unchanged_reextraction_preserves_units_and_history(self):
        first = self.service.extract(self.prepare()["extraction_id"])
        second = self.service.extract(first["extraction_id"])
        self.assertEqual(first["evidence_units"], second["evidence_units"])
        self.assertEqual(first["history"], second["history"])

    def test_new_extractor_version_reprocesses_unchanged_source_and_preserves_revision(self):
        first = self.service.extract(self.prepare()["extraction_id"])
        path = self.campaign_root / "evidence_extraction" / f"{first['extraction_id']}.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        for unit in legacy["evidence_units"]:
            unit["extraction_method"] = "deterministic-html-block-v1"
        legacy["evidence_units"][0]["review_state"] = "approved"
        legacy["evidence_units"][0]["reviewer_decision"] = "approved"
        legacy["evidence_units"][0]["reviewer_notes"] = "Human-reviewed legacy evidence"
        path.write_text(json.dumps(legacy), encoding="utf-8")

        refreshed = self.service.extract(first["extraction_id"])

        self.assertEqual(len(refreshed["evidence_revisions"]), 1)
        self.assertTrue(all(unit["extraction_method"] == self.service.EXTRACTION_METHOD
                            for unit in refreshed["evidence_units"]))
        self.assertEqual({unit["evidence_id"] for unit in refreshed["evidence_units"]},
                         {unit["evidence_id"] for unit in first["evidence_units"]})
        self.assertTrue(all(unit["review_state"] == "proposed"
                            for unit in refreshed["evidence_units"]))
        archived = refreshed["evidence_revisions"][0]
        self.assertEqual(archived["evidence_units"][0]["review_state"], "approved")
        self.assertEqual(archived["evidence_units"][0]["reviewer_notes"],
                         "Human-reviewed legacy evidence")
        self.assertEqual(archived["extraction_methods"], ["deterministic-html-block-v1"])
        reextracted_event = next(
            event for event in refreshed["history"] if event["event"] == "evidence_reextracted"
        )
        self.assertEqual(reextracted_event["extraction_method"],
                         self.service.EXTRACTION_METHOD)
        self.assertEqual(refreshed["history"][-1]["event"], "candidacy_recommended")

    def test_reextraction_state_only_exposes_governed_staleness(self):
        current = self.service.extract(self.prepare()["extraction_id"])
        self.assertFalse(self.service.reextraction_state(current)["available"])
        legacy = json.loads(json.dumps(current))
        for unit in legacy["evidence_units"]:
            unit["extraction_method"] = "deterministic-html-block-v1"
        state = self.service.reextraction_state(legacy)
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], "extractor_version")
        self.assertEqual(state["current_method"], self.service.EXTRACTION_METHOD)

    def test_reextract_removes_legacy_boilerplate_and_is_idempotent(self):
        self.validator.html = """<html><body><main>
        <h2>Description</h2><p>Use Resolve-DnsName to query the configured DNS server for a host name.</p>
        <h2>Feedback</h2><p>Want to try using Ask Learn to clarify or guide you through this topic?</p>
        </main></body></html>"""
        current = self.service.extract(self.prepare()["extraction_id"])
        substantive_id = current["evidence_units"][0]["evidence_id"]
        path = self.campaign_root / "evidence_extraction" / f"{current['extraction_id']}.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy["evidence_units"][0]["extraction_method"] = "deterministic-html-block-v1"
        boilerplate = json.loads(json.dumps(legacy["evidence_units"][0]))
        boilerplate.update(
            evidence_id="EVD-BOILERPLATE", extraction_method="deterministic-html-block-v1",
            normalized_claim="Want to try using Ask Learn to clarify or guide you through this topic?",
            supporting_passage="Want to try using Ask Learn to clarify or guide you through this topic?",
        )
        legacy["evidence_units"].append(boilerplate)
        path.write_text(json.dumps(legacy), encoding="utf-8")

        refreshed = self.service.reextract(current["extraction_id"])
        self.assertEqual([unit["evidence_id"] for unit in refreshed["evidence_units"]],
                         [substantive_id])
        self.assertTrue(all(unit["review_state"] == "proposed"
                            for unit in refreshed["evidence_units"]))
        self.assertEqual(refreshed["evidence_units"][0]["provenance"]["source_candidate_id"],
                         "KSC-AAAAAAAAAAAA")
        revision_count = len(refreshed["evidence_revisions"])
        history_count = len(refreshed["history"])
        repeated = self.service.reextract(current["extraction_id"])
        self.assertEqual(len(repeated["evidence_revisions"]), revision_count)
        self.assertEqual(len(repeated["history"]), history_count)

    def test_evidence_workspace_exposes_only_version_justified_reextraction(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        legacy = json.loads(json.dumps(package))
        for unit in legacy["evidence_units"]:
            unit["extraction_method"] = "deterministic-html-block-v1"
        mocked = Mock()
        mocked.get.return_value = legacy
        mocked.review_workspace.return_value = self.service.review_workspace(
            legacy["extraction_id"]
        ) | {"package": legacy}
        mocked.reextraction_state.return_value = self.service.reextraction_state(legacy)
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked):
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Run Extraction Again", html)
        self.assertIn("deterministic-html-block-v1", html)
        self.assertIn("deterministic-html-block-v3", html)
        self.assertIn(
            f"/curator/growth/evidence-extraction/{package['extraction_id']}/reextract", html
        )

        mocked.get.return_value = package
        mocked.review_workspace.return_value = self.service.review_workspace(
            package["extraction_id"]
        )
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked):
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            )
        self.assertNotIn("Run Extraction Again", response.get_data(as_text=True))

    def test_review_workspace_projects_authoritative_progress_context_and_filters(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first, second = package["evidence_units"][:2]
        self.review_evidence(package["extraction_id"], first["evidence_id"],
                                     "approved", "Verified against the source")
        self.review_evidence(package["extraction_id"], second["evidence_id"],
                                     "needs_revision", "Needs narrower wording")

        workspace = self.service.review_workspace(
            package["extraction_id"], review_state="needs_revision",
            evidence_type=second["evidence_type"],
        )

        self.assertEqual(workspace["counts"]["reviewed"], 2)
        self.assertEqual(workspace["counts"]["remaining"],
                         len(package["evidence_units"]) - 2)
        self.assertEqual([unit["evidence_id"] for unit in workspace["units"]],
                         [second["evidence_id"]])
        self.assertEqual(workspace["context"]["campaign_title"],
                         "Windows Connectivity Production Coverage Review")
        self.assertEqual(workspace["context"]["area"], "DNS")
        self.assertEqual(workspace["context"]["work_type"], "safety_review")
        self.assertFalse(workspace["complete"])

    def test_review_assistance_is_deterministic_read_only_and_not_a_human_decision(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        path = self.campaign_root / "evidence_extraction" / f"{package['extraction_id']}.json"
        before = path.read_bytes()
        phase_six_input_before = self.service.approved_units_for(["KRP-AAAAAAAAAAAA"])

        first = self.service.review_workspace(package["extraction_id"])
        second = self.service.review_workspace(package["extraction_id"])

        self.assertEqual(
            [unit["review_assistance"] for unit in first["units"]],
            [unit["review_assistance"] for unit in second["units"]],
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]),
                         phase_six_input_before)
        self.assertEqual(first["counts"]["reviewed"], 0)
        self.assertEqual(first["counts"]["remaining"], 0)
        self.assertEqual(first["unresolved_candidacy"], len(package["evidence_units"]))
        self.assertTrue(all(unit["review_state"] == "proposed"
                            for unit in first["package"]["evidence_units"]))
        self.assertTrue(all(unit["review_assistance"]["explanation"]
                            for unit in first["units"]))

    def test_assistance_responds_to_authoritative_context_without_fake_strong_match(self):
        unit = {
            "normalized_claim": "A DNS server address is displayed.",
            "supporting_passage": "The interface includes configured DNS server addresses.",
            "evidence_type": "diagnostic_observations",
            "source_location": {"heading": "Configuration"},
            "platform_applicability": "Windows",
        }
        dns = self.service.evidence_review_assistance(unit, {
            "area": "DNS", "gap_summary": "DNS has missing safety.", "platform": "Windows",
        })
        storage = self.service.evidence_review_assistance(unit, {
            "area": "Storage", "gap_summary": "Storage has missing safety.",
            "platform": "Windows",
        })
        ambiguous = self.service.evidence_review_assistance({
            "normalized_claim": "General information", "supporting_passage": "Read the page.",
            "evidence_type": "unspecified", "source_location": {},
            "platform_applicability": "Windows",
        }, {"area": "DNS", "gap_summary": "DNS safety", "platform": "Windows"})

        self.assertNotEqual(dns["category"], "strongly_relevant")
        self.assertNotEqual(storage["category"], "strongly_relevant")
        self.assertEqual(ambiguous["category"], "human_interpretation")
        self.assertNotIn("%", dns["label"])

    def test_dns_safety_extraction_suppresses_non_substantive_units_with_provenance(self):
        self.validator.html = """<html><body><main>
        <h2>Chapter 16 - Troubleshooting TCP/IP</h2>
        <p>For more information, see:</p>
        <p>Last updated on 2026-02-12</p>
        <p>To manually reset TCP/IP, follow these steps:</p>
        <p>Run this command only from an elevated prompt because it resets the TCP/IP
        configuration and can interrupt active network connectivity.</p>
        </main></body></html>"""
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])

        suppressed = workspace["suppressed_units"]
        self.assertEqual({unit["normalized_claim"] for unit in suppressed}, {
            "For more information, see:", "Last updated on 2026-02-12",
        })
        self.assertEqual(workspace["suppressed_count"], 2)
        self.assertEqual(workspace["unresolved_candidacy"], 2)
        self.assertTrue(all(unit["provenance"]["source_candidate_id"] ==
                            "KSC-AAAAAAAAAAAA" for unit in suppressed))
        self.assertTrue(all(unit["content_disposition"]["rule_version"]
                            for unit in suppressed))

    def test_missing_safety_does_not_promote_generic_dns_or_tcpip_procedure(self):
        context = {"area": "DNS", "gap_type": "missing_safety",
                   "work_type": "safety_review", "facet": "safety_authorization",
                   "gap_summary": "DNS has missing safety.", "platform": "Windows"}
        generic = {"normalized_claim": "To manually reset TCP/IP, follow these steps:",
                   "supporting_passage": "To manually reset TCP/IP, follow these steps:",
                   "evidence_type": "procedure", "source_location": {"heading": "Reset TCP/IP"},
                   "platform_applicability": "Windows", "fingerprint": "generic"}
        safety = {"normalized_claim": "Run the DNS reset from an elevated prompt; this can interrupt connectivity.",
                  "supporting_passage": "Administrator privileges are required for the DNS reset "
                                        "and active network connections can be interrupted.",
                  "evidence_type": "safety", "source_location": {"heading": "Important"},
                  "platform_applicability": "Windows", "fingerprint": "safety"}

        self.assertNotEqual(self.service.candidacy_recommendation(
            generic, context)["machine_recommended_role"], "candidate")
        self.assertEqual(self.service.candidacy_recommendation(
            safety, context)["machine_recommended_role"], "candidate")
        self.assertNotEqual(self.service.candidate_purpose(generic, context)["category"],
                            "Diagnostic verification")

    def test_bulk_context_assignment_is_scoped_human_initiated_and_auditable(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="context", human_role="unresolved")
        expected = workspace["bulk_context_eligible_count"]
        self.assertGreater(expected, 0)

        updated = self.service.bulk_assign_visible_machine_context(
            package["extraction_id"], machine_recommendation="context",
            human_role="unresolved", expected_count=expected)

        assigned = [unit for unit in updated["evidence_units"]
                    if (unit.get("candidacy") or {}).get("human_confirmed_role") == "context"]
        self.assertEqual(len(assigned), expected)
        self.assertTrue(all((unit.get("candidacy") or {}).get("machine_recommended_role") ==
                            "context" for unit in assigned))
        self.assertTrue(all(unit.get("review_state") == "proposed" for unit in assigned))
        self.assertEqual(updated["history"][-1]["event"], "machine_context_bulk_assigned")
        self.assertEqual(updated["history"][-1]["count"], expected)
        self.assertNotEqual(updated["candidacy"]["candidate_set_status"], "confirmed")

    def test_bulk_context_route_requires_explicit_count_and_preserves_filter_scope(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="context",
            human_role="unresolved")
        expected = workspace["bulk_context_eligible_count"]
        self.assertGreater(expected, 0)

        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                "/candidacy/bulk-context",
                data={"expected_count": str(expected), "review_state": "all",
                      "evidence_type": "all", "assistance": "all",
                      "machine_recommendation": "context", "human_role": "unresolved"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("machine_recommendation=context", response.location)
        updated = self.service.get(package["extraction_id"])
        self.assertEqual(updated["history"][-1]["event"],
                         "machine_context_bulk_assigned")
        self.assertEqual(updated["history"][-1]["count"], expected)

    def test_package_wide_context_assignment_needs_no_filter_manipulation(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        expected = workspace["all_bulk_context_eligible_count"]
        self.assertGreater(expected, 0)

        updated = self.service.bulk_assign_all_machine_context(
            package["extraction_id"], expected_count=expected)

        assigned = [unit for unit in updated["evidence_units"]
                    if (unit.get("candidacy") or {}).get("human_confirmed_role") == "context"]
        self.assertEqual(len(assigned), expected)
        self.assertTrue(all((unit.get("candidacy") or {}).get("machine_recommended_role") ==
                            "context" for unit in assigned))
        self.assertTrue(all(unit.get("review_state") == "proposed" for unit in assigned))
        self.assertEqual(updated["history"][-1]["event"],
                         "machine_context_bulk_assigned_all")
        self.assertEqual(updated["history"][-1]["count"], expected)
        refreshed = self.service.review_workspace(package["extraction_id"])
        self.assertEqual(refreshed["all_bulk_context_eligible_count"], 0)
        self.assertEqual(refreshed["unresolved_candidacy"],
                         workspace["unresolved_candidacy"] - expected)

    def test_package_wide_context_assignment_preserves_human_and_evidence_decisions(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        contexts = [unit for unit in package["evidence_units"]
                    if self.service._is_reviewable(unit)][:3]
        self.assertEqual(len(contexts), 3)
        for unit in contexts:
            unit["candidacy"]["machine_recommended_role"] = "context"
        contexts[0]["candidacy"]["human_confirmed_role"] = "candidate"
        contexts[0]["candidacy"]["role_decided_by"] = "Human"
        contexts[1]["review_state"] = "approved"
        contexts[1]["reviewer_decision"] = "approved"
        self.service._save(package)
        workspace = self.service.review_workspace(package["extraction_id"])

        updated = self.service.bulk_assign_all_machine_context(
            package["extraction_id"],
            expected_count=workspace["all_bulk_context_eligible_count"],
        )

        by_id = {unit["evidence_id"]: unit for unit in updated["evidence_units"]}
        self.assertEqual(by_id[contexts[0]["evidence_id"]]["candidacy"]
                         ["human_confirmed_role"], "candidate")
        self.assertEqual(by_id[contexts[1]["evidence_id"]]["review_state"], "approved")
        self.assertEqual(by_id[contexts[1]["evidence_id"]]["reviewer_decision"],
                         "approved")
        self.assertEqual(by_id[contexts[1]["evidence_id"]]["candidacy"]
                         ["human_confirmed_role"], "context")

    def test_reviewed_machine_context_with_unresolved_human_role_renders_package_action(self):
        """Evidence decisions do not resolve the separate candidacy-role decision."""
        package = self.service.extract(self.prepare()["extraction_id"])
        contexts = [unit for unit in package["evidence_units"]
                    if self.service._is_reviewable(unit)][:2]
        self.assertEqual(len(contexts), 2)
        for unit in contexts:
            unit["candidacy"]["machine_recommended_role"] = "context"
            unit["review_state"] = "approved"
            unit["reviewer_decision"] = "approved"
            unit["reviewer_notes"] = "Previously reviewed evidence"
        self.service._save(package)

        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertGreaterEqual(workspace["all_bulk_context_eligible_count"], 2)
        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Assign all Machine Context as Reviewer Context", html)
        self.assertIn(f"({workspace['all_bulk_context_eligible_count']})", html)

        updated = self.service.bulk_assign_all_machine_context(
            package["extraction_id"],
            expected_count=workspace["all_bulk_context_eligible_count"],
        )
        by_id = {unit["evidence_id"]: unit for unit in updated["evidence_units"]}
        for unit in contexts:
            current = by_id[unit["evidence_id"]]
            self.assertEqual(current["candidacy"]["human_confirmed_role"], "context")
            self.assertEqual(current["review_state"], "approved")
            self.assertEqual(current["reviewer_decision"], "approved")
            self.assertEqual(current["reviewer_notes"], "Previously reviewed evidence")

    def test_package_wide_context_route_is_explicit_and_returns_to_refreshed_workspace(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        expected = self.service.review_workspace(
            package["extraction_id"])["all_bulk_context_eligible_count"]

        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                "/candidacy/bulk-context-all",
                data={"expected_count": str(expected)},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"Assigned+{expected}+machine-Context", response.location)
        self.assertEqual(self.service.review_workspace(
            package["extraction_id"])["all_bulk_context_eligible_count"], 0)

    def test_detail_exposes_package_wide_context_action_and_clear_filter_feedback(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(
            package["extraction_id"], machine_recommendation="context")
        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                "?review_state=all&machine_recommendation=context"
            )
        html = response.get_data(as_text=True)

        self.assertIn("Assign all Machine Context as Reviewer Context", html)
        self.assertIn(f"({workspace['all_bulk_context_eligible_count']})", html)
        self.assertIn("Active filters", html)
        self.assertIn(f"{len(workspace['units'])} of {workspace['reviewable_count']} reviewable units shown",
                      html)
        self.assertTrue(all(unit["machine_recommendation"] == "context"
                            for unit in workspace["units"]))

    def test_legacy_confirmed_candidate_set_remains_current_after_rule_upgrade(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            self.service.set_candidacy_role(package["extraction_id"], unit["evidence_id"],
                                            "candidate")
        confirmed = self.service.confirm_candidate_set(package["extraction_id"])
        legacy_rule = "deterministic-evidence-candidacy-v1"
        confirmed["candidacy"]["rule_version"] = legacy_rule
        confirmed["candidacy"]["confirmation_fingerprint"] = \
            self.service._candidate_set_fingerprint(confirmed, rule_version=legacy_rule)
        self.service._save(confirmed)

        self.assertTrue(self.service._candidate_set_current(
            self.service.get(package["extraction_id"])))

    def test_candidate_purpose_is_deterministic_explained_and_conservative(self):
        context = {"area": "DNS", "gap_summary": "DNS has missing safety.",
                   "platform": "Windows"}
        dns = {"normalized_claim": "The cmdlet returns configured DNS servers.",
               "supporting_passage": "Network configuration includes DNS server addresses.",
               "evidence_type": "diagnostic_observations",
               "source_location": {"heading": "Description"}}
        command = {"normalized_claim": "Get-NetIPConfiguration -InterfaceIndex 12",
                   "supporting_passage": "Gets configuration for the selected interface.",
                   "evidence_type": "commands",
                   "source_location": {"heading": "Interface index"}}
        ambiguous = {"normalized_claim": "Additional information.",
                     "supporting_passage": "Read the documentation.",
                     "evidence_type": "unspecified", "source_location": {}}

        first = self.service.candidate_purpose(dns, context)
        self.assertEqual(first, self.service.candidate_purpose(deepcopy(dns), deepcopy(context)))
        self.assertEqual(first["category"], "Configuration verification")
        self.assertIn("DNS has missing safety", first["explanation"])
        self.assertEqual(self.service.candidate_purpose(command, context)["category"],
                         "Scope/target selection")
        self.assertIn("command", self.service.candidate_purpose(command, context)["explanation"])
        self.assertEqual(self.service.candidate_purpose(ambiguous, context)["category"],
                         "Human interpretation required")
        self.assertTrue(self.service.candidate_purpose(ambiguous, context)["explanation"])

    def test_candidate_purpose_projection_is_read_only_and_candidate_only(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        path = self.campaign_root / "evidence_extraction" / f"{package['extraction_id']}.json"
        before = path.read_bytes()
        phase_six_before = self.service.approved_units_for(["KRP-AAAAAAAAAAAA"])

        workspace = self.service.review_workspace(package["extraction_id"])
        repeated = self.service.review_workspace(package["extraction_id"])

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]),
                         phase_six_before)
        self.assertEqual([unit["candidate_purpose"] for unit in workspace["units"]],
                         [unit["candidate_purpose"] for unit in repeated["units"]])
        for unit in workspace["units"]:
            if unit["machine_recommendation"] == "candidate":
                self.assertIsNotNone(unit["candidate_purpose"])
                self.assertTrue(unit["candidate_purpose"]["explanation"])
            else:
                self.assertIsNone(unit["candidate_purpose"])
        self.assertTrue(all(unit["review_state"] == "proposed"
                            for unit in workspace["package"]["evidence_units"]))
        self.assertTrue(all((unit.get("candidacy") or {}).get("human_confirmed_role") is None
                            for unit in workspace["package"]["evidence_units"]))
        self.assertFalse(workspace["candidate_set_current"])
        self.assertEqual(workspace["machine_recommendation_counts"],
                         repeated["machine_recommendation_counts"])
        self.assertEqual(workspace["human_role_counts"], repeated["human_role_counts"])

    def test_candidate_purpose_ui_is_advisory_and_adds_no_bulk_action(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        unit = workspace["units"][0]
        unit["machine_recommendation"] = "candidate"
        unit["candidacy"]["machine_recommended_role"] = "candidate"
        unit["candidate_purpose"] = self.service.candidate_purpose(
            unit, workspace["context"])
        workspace["units"] = [unit]
        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            html = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            ).get_data(as_text=True)
        self.assertIn("Candidate Purpose", html)
        self.assertIn("Why this may matter", html)
        self.assertNotIn("Assign All", html)
        self.assertNotIn("Approve All", html)
        self.assertNotIn("LLM", html)

    def test_assistance_filter_preserves_individual_units_and_next_undecided(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.confirm_all_candidates(package["extraction_id"])
        all_units = self.service.review_workspace(package["extraction_id"])
        category = all_units["units"][0]["review_assistance"]["category"]
        filtered = self.service.review_workspace(
            package["extraction_id"], review_state="proposed", assistance=category,
        )

        self.assertTrue(filtered["units"])
        self.assertTrue(all(unit["review_assistance"]["category"] == category
                            for unit in filtered["units"]))
        self.assertEqual(filtered["next_undecided"], filtered["units"][0]["evidence_id"])
        self.assertEqual(sum(all_units["assistance_counts"].values()),
                         len(package["evidence_units"]))
        visible = {
            unit["evidence_id"]
            for assistance in all_units["assistance_categories"]
            for unit in self.service.review_workspace(
                package["extraction_id"], assistance=assistance
            )["units"]
        }
        self.assertEqual(visible, {unit["evidence_id"] for unit in package["evidence_units"]})

    def test_review_workspace_completion_requires_every_individual_decision(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for index, unit in enumerate(package["evidence_units"]):
            decision = "approved" if index == 0 else "rejected"
            self.review_evidence(package["extraction_id"], unit["evidence_id"],
                                         decision, f"note-{index}")
        workspace = self.service.review_workspace(package["extraction_id"])
        self.assertTrue(workspace["complete"])
        self.assertEqual(workspace["counts"]["remaining"], 0)
        self.assertEqual(workspace["counts"]["reviewed"], workspace["counts"]["total"])
        self.assertEqual(workspace["package"]["evidence_units"][0]["reviewer_notes"], "note-0")

    def test_detail_renders_compact_traceability_filters_and_campaign_navigation(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.confirm_all_candidates(package["extraction_id"])
        mocked = Mock()
        mocked.review_workspace.return_value = self.service.review_workspace(package["extraction_id"])
        mocked.reextraction_state.return_value = self.service.reextraction_state(package)
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}?review_state=proposed"
            )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("0 of", html)
        self.assertIn("Back to Campaign", html)
        self.assertIn("Windows Connectivity Production Coverage Review", html)
        self.assertIn("Next undecided", html)
        self.assertIn("Traceability details", html)
        self.assertIn("Review assistance", html)
        self.assertIn("Potential role", html)
        self.assertIn("All assistance categories", html)
        self.assertIn(package["evidence_units"][0]["evidence_id"], html)
        self.assertNotIn("Approve All", html)
        self.assertNotIn("Continue to Workflow Claim Planning", html)

    def test_individual_review_redirects_to_next_undecided_without_bulk_decision(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first, second = package["evidence_units"][:2]
        mocked = Mock()
        changed = json.loads(json.dumps(package))
        changed["evidence_units"][0]["review_state"] = "approved"
        changed["evidence_units"][0]["reviewer_notes"] = "Only this unit"
        mocked.review_evidence.return_value = changed
        post_decision_workspace = self.service.review_workspace(
            package["extraction_id"], review_state="all", evidence_type="all",
            assistance="all",
        )
        post_decision_workspace["next_undecided"] = second["evidence_id"]
        mocked.review_workspace.return_value = post_decision_workspace
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}/evidence/{first['evidence_id']}",
                data={"decision": "approved", "notes": "Only this unit"},
            )
        mocked.review_evidence.assert_called_once_with(
            package["extraction_id"], first["evidence_id"], "approved", "Only this unit"
        )
        self.assertIn("review_state=all", response.headers["Location"])
        self.assertTrue(response.headers["Location"].endswith(f"#evidence-{second['evidence_id']}"))

    def test_approve_preserves_each_filter_and_keeps_next_inside_filtered_group(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.confirm_all_candidates(package["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        category = next(category for category, count in workspace["assistance_counts"].items()
                        if count >= 2)
        group = self.service.review_workspace(
            package["extraction_id"], review_state="proposed",
            evidence_type="all", assistance=category,
        )["units"]
        first, second = group[:2]
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                f"/evidence/{first['evidence_id']}",
                data={"decision": "approved", "notes": "Reviewed",
                      "review_state": "proposed", "evidence_type": "all",
                      "assistance": category},
            )
        location = response.headers["Location"]
        self.assertIn("review_state=proposed", location)
        self.assertIn("evidence_type=all", location)
        self.assertIn(f"assistance={category}", location)
        self.assertTrue(location.endswith(f"#evidence-{second['evidence_id']}"))

    def test_role_assignment_preserves_split_filters_and_advances_within_group(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        recommendation = max(workspace["machine_recommendation_counts"],
                             key=workspace["machine_recommendation_counts"].get)
        group = self.service.review_workspace(
            package["extraction_id"], machine_recommendation=recommendation,
            human_role="unresolved",
        )["units"]
        self.assertGreaterEqual(len(group), 2)
        first, second = group[:2]
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                f"/evidence/{first['evidence_id']}/candidacy",
                data={"role": "candidate", "review_state": "all",
                      "evidence_type": "all", "assistance": "all",
                      "machine_recommendation": recommendation,
                      "human_role": "unresolved",
                      "return_url": "https://evil.example/redirect"},
            )
        location = response.headers["Location"]
        self.assertIn(f"machine_recommendation={recommendation}", location)
        self.assertIn("human_role=unresolved", location)
        self.assertTrue(location.endswith(f"#evidence-{second['evidence_id']}"))
        self.assertNotIn("evil.example", location)
        current = self.service.get(package["extraction_id"])
        assigned = next(unit for unit in current["evidence_units"]
                        if unit["evidence_id"] == first["evidence_id"])
        untouched = next(unit for unit in current["evidence_units"]
                         if unit["evidence_id"] == second["evidence_id"])
        self.assertEqual(assigned["candidacy"]["human_confirmed_role"], "candidate")
        self.assertIsNone(untouched["candidacy"]["human_confirmed_role"])
        self.assertEqual(assigned["review_state"], "proposed")

    def test_final_split_filter_role_assignment_renders_filtered_group_completion(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        recommendation = max(workspace["machine_recommendation_counts"],
                             key=workspace["machine_recommendation_counts"].get)
        group = self.service.review_workspace(
            package["extraction_id"], machine_recommendation=recommendation,
            human_role="unresolved",
        )["units"]
        for unit in group[:-1]:
            self.service.set_candidacy_role(
                package["extraction_id"], unit["evidence_id"], "context")
        last = group[-1]
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                f"/evidence/{last['evidence_id']}/candidacy",
                data={"role": "context", "review_state": "all",
                      "evidence_type": "all", "assistance": "all",
                      "machine_recommendation": recommendation,
                      "human_role": "unresolved"},
                follow_redirects=True,
            )
        html = response.get_data(as_text=True)
        projected = self.service.review_workspace(
            package["extraction_id"], machine_recommendation=recommendation,
            human_role="unresolved")
        self.assertTrue(projected["group_complete"])
        self.assertFalse(projected["candidate_set_current"])
        self.assertGreater(projected["unresolved_candidacy"], 0)
        self.assertIn("Filtered group complete", html)
        self.assertIn("does not confirm the package candidate set", html)
        self.assertNotIn("Evidence review complete", html)
        self.assertNotIn("Accept All", html)

    def test_reject_and_needs_revision_preserve_multiple_filters(self):
        for decision in ("rejected", "needs_revision"):
            with self.subTest(decision=decision):
                package = self.service.extract(self.prepare()["extraction_id"])
                self.confirm_all_candidates(package["extraction_id"])
                unit = next(unit for unit in self.service.review_workspace(
                    package["extraction_id"]
                )["units"] if unit["review_state"] == "proposed")
                category = unit["review_assistance"]["category"]
                evidence_type = unit["evidence_type"]
                with patch("app.app.KnowledgeEvidenceExtractionService",
                           return_value=self.service):
                    response = flask_app.test_client().post(
                        f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                        f"/evidence/{unit['evidence_id']}",
                        data={"decision": decision, "notes": "Reviewed",
                              "review_state": "proposed", "evidence_type": evidence_type,
                              "assistance": category},
                    )
                location = response.headers["Location"]
                self.assertIn("review_state=proposed", location)
                self.assertIn(f"evidence_type={evidence_type}", location)
                self.assertIn(f"assistance={category}", location)

    def test_final_filtered_decision_preserves_group_and_renders_group_completion(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.confirm_all_candidates(package["extraction_id"])
        workspace = self.service.review_workspace(package["extraction_id"])
        evidence_type = next(kind for kind in workspace["evidence_types"]
                             if 0 < sum(unit["evidence_type"] == kind
                                        for unit in workspace["units"])
                             < workspace["counts"]["total"])
        group = self.service.review_workspace(
            package["extraction_id"], evidence_type=evidence_type,
        )["units"]
        for unit in group[:-1]:
            self.review_evidence(package["extraction_id"], unit["evidence_id"],
                                         "approved", "Reviewed")
        last = group[-1]
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = False
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                f"/evidence/{last['evidence_id']}",
                data={"decision": "approved", "notes": "Reviewed",
                      "review_state": "proposed", "evidence_type": "all",
                      "assistance": "all"} | {"evidence_type": evidence_type},
                follow_redirects=True,
            )
        html = response.get_data(as_text=True)
        current = self.service.review_workspace(
            package["extraction_id"], review_state="proposed",
            evidence_type=evidence_type,
        )
        self.assertTrue(current["group_complete"])
        self.assertFalse(current["complete"])
        self.assertGreater(current["counts"]["remaining"], 0)
        self.assertIn("Filtered group complete", html)
        self.assertIn("does not confirm the package candidate set", html)
        self.assertIn("View All Undecided", html)
        self.assertIn("Clear Filters", html)
        self.assertNotIn("Evidence review complete", html)
        self.assertNotIn("Approve All", html)

    def test_invalid_filters_are_normalized_and_cannot_inject_redirect(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.confirm_all_candidates(package["extraction_id"])
        unit = package["evidence_units"][0]
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=self.service):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
                f"/evidence/{unit['evidence_id']}",
                data={"decision": "approved", "review_state": "https://evil.example",
                      "evidence_type": "../../bad", "assistance": "bad",
                      "return_url": "https://evil.example/redirect"},
            )
        location = response.headers["Location"]
        self.assertTrue(location.startswith(
            f"/curator/growth/evidence-extraction/{package['extraction_id']}?"
        ))
        self.assertIn("review_state=all", location)
        self.assertIn("evidence_type=all", location)
        self.assertIn("assistance=all", location)
        self.assertNotIn("evil.example", location)

    def test_completion_continuation_is_exposed_only_when_backend_allows_it(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        for unit in package["evidence_units"]:
            self.review_evidence(package["extraction_id"], unit["evidence_id"], "approved")
        workspace = self.service.review_workspace(package["extraction_id"])
        mocked = Mock()
        mocked.review_workspace.return_value = workspace
        mocked.reextraction_state.return_value = self.service.reextraction_state(workspace["package"])
        with (patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked),
              patch("app.app.KnowledgeClaimPlanningService") as claim_service):
            claim_service.return_value.workflow_is_eligible.return_value = True
            html = flask_app.test_client().get(
                f"/curator/growth/evidence-extraction/{package['extraction_id']}"
            ).get_data(as_text=True)
        self.assertIn("Evidence review complete", html)
        self.assertIn("Continue to Workflow Claim Planning", html)
        self.assertIn(
            f"/curator/growth/coverage-campaigns/{package['campaign_id']}/work-items/"
            f"{package['work_item_id']}/workflow-claim-planning", html
        )

    def test_reextraction_route_uses_service_and_authoritative_return(self):
        extraction_id = "KEX-AAAAAAAAAAAA"
        mocked = Mock()
        mocked.reextraction_state.return_value = {"available": True}
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{extraction_id}/reextract"
            )
        mocked.reextract.assert_called_once_with(extraction_id)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/curator/growth/evidence-extraction/{extraction_id}",
                      response.headers["Location"])

    def test_initial_extraction_route_cannot_bypass_reextraction_guard(self):
        extraction_id = "KEX-AAAAAAAAAAAA"
        mocked = Mock()
        mocked.get.return_value = {"status": "needs_review"}
        with patch("app.app.KnowledgeEvidenceExtractionService", return_value=mocked):
            response = flask_app.test_client().post(
                f"/curator/growth/evidence-extraction/{extraction_id}/run"
            )
        mocked.extract.assert_not_called()
        self.assertIn("governed+re-extraction", response.headers["Location"])

    def test_source_change_marks_stale_without_destroying_approved_evidence(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        unit = package["evidence_units"][0]
        self.review_evidence(package["extraction_id"], unit["evidence_id"], "approved")
        self.validator.digest = "digest-2"
        stale = self.service.refresh_status(package["extraction_id"])
        self.assertEqual(stale["status"], "needs_refresh")
        self.assertTrue(any(value["evidence_id"] == unit["evidence_id"]
                            for value in stale["evidence_units"]))
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_changed_reextraction_preserves_prior_revision(self):
        first = self.service.extract(self.prepare()["extraction_id"])
        self.validator.digest = "digest-2"
        self.validator.html = self.validator.html.replace("Open Settings", "Open Windows Settings")
        second = self.service.extract(first["extraction_id"])
        self.assertEqual(len(second["evidence_revisions"]), 1)
        self.assertEqual(second["evidence_revisions"][0]["source_fingerprint"], "digest-1")
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in second["evidence_units"]))

    def test_unrelated_redirect_and_failed_retrieval_are_governed_failures(self):
        package = self.prepare()
        self.validator.final_url = "https://example.com/unrelated"
        failed = self.service.extract(package["extraction_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("unrelated", failed["retrieval"]["reason"])

    def test_extractor_preserves_modal_language_without_stronger_inference(self):
        self.validator.html = "<html><body><main><p>This change may help resolve the connection problem.</p></main></body></html>"
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertIn("may help", package["evidence_units"][0]["normalized_claim"])
        self.assertNotIn("will fix", package["evidence_units"][0]["normalized_claim"])

    def test_extractor_excludes_page_ui_feedback_navigation_and_generic_metadata(self):
        self.validator.html = """<html><body>
        <p>Access to this page requires authorization. You can try signing in or changing directories.</p>
        <main>
          <h2>Description</h2>
          <p>The Get-NetIPConfiguration cmdlet gets usable interfaces, IP addresses, and DNS servers.</p>
          <h2>Example 1: Get the IP configuration</h2>
          <pre>PS C:\\&gt;Get-NetIPConfiguration</pre>
          <p>This command gets IP configuration information for connected interfaces.</p>
          <h2>CommonParameters</h2>
          <p>This cmdlet supports the common parameters Debug, ErrorAction, and WarningAction.</p>
          <h2>Related Links</h2>
          <ul><li>Get-DNSClientServerAddress reference</li></ul>
          <h2>Feedback</h2>
          <p>Need help with this topic?</p>
          <p>Want to try using Ask Learn to clarify or guide you through this topic?</p>
        </main></body></html>"""
        package = self.service.extract(self.prepare()["extraction_id"])
        extracted = " ".join(unit["normalized_claim"] for unit in package["evidence_units"])
        self.assertIn("DNS servers", extracted)
        self.assertIn("Get-NetIPConfiguration", extracted)
        self.assertNotIn("requires authorization", extracted)
        self.assertNotIn("common parameters", extracted)
        self.assertNotIn("Get-DNSClientServerAddress", extracted)
        self.assertNotIn("Need help with this topic", extracted)
        self.assertNotIn("Ask Learn", extracted)

    def test_boilerplate_filter_preserves_stable_identity_for_substantive_evidence(self):
        self.validator.html = """<html><body><main>
        <h2>Description</h2><p>Use Resolve-DnsName to query the configured DNS server for a host name.</p>
        <h2>Feedback</h2><p>Want to try using Ask Learn to clarify or guide you through this topic?</p>
        </main></body></html>"""
        first = self.service.extract(self.prepare()["extraction_id"])
        self.assertEqual(len(first["evidence_units"]), 1)
        evidence_id = first["evidence_units"][0]["evidence_id"]
        self.validator.digest = "digest-2"
        second = self.service.extract(first["extraction_id"])
        self.assertEqual(second["evidence_units"][0]["evidence_id"], evidence_id)
        self.assertEqual(second["evidence_units"][0]["review_state"], "proposed")


if __name__ == "__main__":
    unittest.main()
