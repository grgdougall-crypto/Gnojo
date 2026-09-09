import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.data_root import APPLICATION_ROOT
from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchService,
)
from app.services.supervised_campaign_autopilot_service import (
    SupervisedCampaignAutopilotError,
    SupervisedCampaignAutopilotService,
)


class Provider:
    model = "triage-model-v1"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def generate_workflow_node_suggestion(self, prompt):
        self.calls += 1
        return deepcopy(self.results.pop(0))


class UnavailableProvider:
    model = "unavailable"

    def generate_workflow_node_suggestion(self, prompt):
        raise RuntimeError("provider unavailable")


class SupervisedCampaignAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign_root = self.root / "knowledge_campaigns"
        self.research_root = self.campaign_root / "research"
        self.research_root.mkdir(parents=True)
        data = self.root / "app" / "data"
        data.mkdir(parents=True)
        (data / "source_authority_policy.json").write_text(json.dumps({
            "schema_version": "1.0", "tiers": [], "research_targets": [],
        }), encoding="utf-8")
        self.taxonomy = data / "knowledge_coverage_taxonomy.json"
        self.taxonomy.write_text(json.dumps({
            "schema_version": "1.0", "domains": [],
        }), encoding="utf-8")
        self.campaign_id = "KCP-AUTOPILOT1"
        self.work_id = "KCW-AUTOPILOT1"
        self.package_id = "KRP-A1B2C3D4E5F6"
        self.candidates = [
            self._candidate("KSC-GOOD0000001", title="Official disk guidance"),
            self._candidate("KSC-AMBIG000001", title="General support article"),
            self._candidate(
                "KSC-BAD00000001", title="Unrelated page",
                topic_relevant=False,
            ),
        ]
        self.package = {
            "schema_version": "1.0", "package_id": self.package_id,
            "campaign_id": self.campaign_id, "gap_id": "KCG-AUTOPILOT1",
            "work_item_id": self.work_id, "target_coverage_area": "storage",
            "coverage_facet": "learning", "status": "ready_for_review",
            "candidate_sources": self.candidates, "selected_sources": [],
            "rejected_sources": [], "research_notes": "", "history": [],
        }
        self.campaign = {
            "campaign_id": self.campaign_id,
            "research_packages": [{
                "package_id": self.package_id, "status": "ready_for_review"
            }],
            "history": [],
        }
        (self.research_root / f"{self.package_id}.json").write_text(
            json.dumps(self.package, indent=2) + "\n", encoding="utf-8"
        )
        (self.campaign_root / f"{self.campaign_id}.json").write_text(
            json.dumps(self.campaign, indent=2) + "\n", encoding="utf-8"
        )
        self.research = KnowledgeSourceResearchService(
            self.root, self.campaign_root,
            policy_path=data / "source_authority_policy.json",
            taxonomy_path=self.taxonomy,
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _candidate(identity, *, title, topic_relevant=True):
        return {
            "source_candidate_id": identity, "page_title": title,
            "canonical_url": f"https://support.example/{identity.lower()}",
            "publisher": "Example Support", "domain": "support.example",
            "authority_tier": 2, "http_status": 200,
            "topic_relevant": topic_relevant,
            "duplicate_status": "new", "relevance_reason": "Storage guidance",
            "review_state": "proposed", "reviewer_notes": "",
        }

    def service(self):
        provider = Provider([
            {"recommendation": "recommended_keep", "confidence": "high",
             "reason": "Directly addresses the governed storage gap."},
            {"recommendation": "recommended_keep", "confidence": "low",
             "reason": "Potentially useful but the scope is broad."},
        ])
        return SupervisedCampaignAutopilotService(
            self.research, [("OpenAI", provider)],
            now=lambda: "2026-09-08T15:00:00+00:00",
        ), provider

    def test_ai_and_deterministic_triage_fail_closed_for_ambiguity(self):
        service, provider = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        by_id = {item["candidate_identity"]: item
                 for item in preview["recommendations"]}
        self.assertEqual(by_id["KSC-GOOD0000001"]["recommendation"],
                         "recommended_keep")
        self.assertEqual(by_id["KSC-AMBIG000001"]["recommendation"],
                         "human_review_required")
        self.assertEqual(by_id["KSC-BAD00000001"]["recommendation"],
                         "recommended_reject")
        self.assertEqual(by_id["KSC-BAD00000001"]["provider"], "Deterministic")
        self.assertEqual(provider.calls, 2)

    def test_preview_is_read_only_and_records_complete_evidence(self):
        service, provider = self.service()
        service.prepare_snapshot(self.package_id)
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        reused = service.prepare_snapshot(self.package_id)
        preview = service.preview(self.package_id)
        second = service.preview(self.package_id)
        self.assertEqual(reused["status"], "snapshot_reused")
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview, second)
        self.assertEqual(provider.calls, 2)
        self.assertTrue(all(item["deterministic_evidence"]
                            for item in preview["recommendations"]))
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_unavailable_ai_leaves_reviewable_candidates_human_required(self):
        service = SupervisedCampaignAutopilotService(
            self.research, [("OpenAI", UnavailableProvider())]
        )
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        by_id = {item["candidate_identity"]: item
                 for item in preview["recommendations"]}
        self.assertEqual(by_id["KSC-GOOD0000001"]["recommendation"],
                         "human_review_required")
        self.assertEqual(by_id["KSC-AMBIG000001"]["recommendation"],
                         "human_review_required")
        self.assertEqual(by_id["KSC-BAD00000001"]["recommendation"],
                         "recommended_reject")

    def test_high_confidence_curation_is_preapplied_and_can_be_overridden(self):
        service, _ = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        self.assertEqual(preview["proposed_decisions"], {
            "KSC-GOOD0000001": "selected",
            "KSC-AMBIG000001": None,
            "KSC-BAD00000001": "rejected",
        })
        self.assertEqual(preview["curated_count"], 2)
        self.assertEqual(preview["human_decision_count"], 1)

        result = service.approve(
            self.package_id,
            {"KSC-GOOD0000001": "rejected",
             "KSC-AMBIG000001": "selected"},
            expected_snapshot_id=preview["snapshot_id"],
            expected_preview_fingerprint=preview["review_fingerprint"],
            reviewer="Reviewer",
        )
        self.assertEqual(result["package"]["selected_sources"],
                         ["KSC-AMBIG000001"])
        self.assertEqual(result["package"]["rejected_sources"],
                         ["KSC-BAD00000001", "KSC-GOOD0000001"])

    def test_duplicate_titles_with_different_urls_remain_distinct(self):
        package = self.research.get(self.package_id)
        package["candidate_sources"][1]["page_title"] = package["candidate_sources"][0]["page_title"]
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, _ = self.service()

        snapshot = service.prepare_snapshot(self.package_id)["snapshot"]

        self.assertEqual(len(snapshot["candidate_identities"]), 3)
        self.assertEqual(len(set(snapshot["candidate_identities"])), 3)
        self.assertEqual(snapshot["approval_blockers"], [])

    def test_equivalent_duplicate_canonical_url_is_one_deterministic_review_subject(self):
        package = self.research.get(self.package_id)
        duplicate = deepcopy(package["candidate_sources"][0])
        duplicate["existing_gnojo_source_match"] = {
            "content_type": "article", "identifier": "another-article",
            "source_path": "knowledge_base/published/another-article.json",
        }
        package["candidate_sources"].insert(1, duplicate)
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, _ = self.service()

        snapshot = service.prepare_snapshot(self.package_id)["snapshot"]

        self.assertEqual(len(snapshot["recommendations"]), 3)
        self.assertEqual(snapshot["approval_blockers"], [])
        recommendation = next(
            item for item in snapshot["recommendations"]
            if item["candidate_identity"] == "KSC-GOOD0000001"
        )
        self.assertEqual(recommendation["equivalent_candidate_count"], 2)
        approved = service.approve(
            self.package_id,
            {"KSC-AMBIG000001": "rejected"},
            expected_snapshot_id=snapshot["snapshot_id"],
            expected_preview_fingerprint=snapshot["review_fingerprint"],
            reviewer="Reviewer",
        )["package"]
        self.assertEqual(approved["selected_sources"], ["KSC-GOOD0000001"])
        duplicate_states = [
            item["review_state"] for item in approved["candidate_sources"]
            if item["source_candidate_id"] == "KSC-GOOD0000001"
        ]
        self.assertEqual(duplicate_states, ["selected", "selected"])

    def test_existing_reuse_and_distinct_candidate_do_not_collide(self):
        package = self.research.get(self.package_id)
        package["candidate_sources"][0]["source_origin"] = "existing_gnojo"
        package["candidate_sources"][0]["existing_gnojo_source_match"] = {
            "content_type": "article", "identifier": "existing-article",
            "source_path": "knowledge_base/published/existing-article.json",
        }
        package["candidate_sources"][1]["source_origin"] = "external"
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, _ = self.service()

        snapshot = service.prepare_snapshot(self.package_id)["snapshot"]

        self.assertEqual(len(snapshot["candidate_identities"]), 3)
        self.assertEqual(snapshot["approval_blockers"], [])

    def test_real_identity_collision_fails_closed_with_candidate_details(self):
        package = self.research.get(self.package_id)
        collision = self._candidate(
            "KSC-GOOD0000001", title="Different authoritative page",
            topic_relevant=False,
        )
        collision["canonical_url"] = "https://support.example/different-page"
        package["candidate_sources"].append(collision)
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, _ = self.service()

        snapshot = service.prepare_snapshot(self.package_id)["snapshot"]

        message = " ".join(snapshot["approval_blockers"])
        self.assertIn("Candidate identity conflict", message)
        self.assertIn("Official disk guidance", message)
        self.assertIn("Different authoritative page", message)
        self.assertIn("different canonical URLs", message)

    def test_high_confidence_curation_fails_closed_without_authority_evidence(self):
        package = self.research.get(self.package_id)
        package["candidate_sources"][0]["publisher"] = None
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, _ = self.service()

        preview = service.prepare_snapshot(self.package_id)["snapshot"]

        by_id = {item["candidate_identity"]: item
                 for item in preview["recommendations"]}
        self.assertEqual(by_id["KSC-GOOD0000001"]["recommendation"],
                         "human_review_required")
        self.assertIsNone(
            preview["proposed_decisions"]["KSC-GOOD0000001"]
        )

    def test_explicit_reanalysis_supersedes_snapshot_without_erasing_history(self):
        service, provider = self.service()
        first = service.prepare_snapshot(self.package_id)["snapshot"]
        provider.results = [
            {"recommendation": "recommended_reject", "confidence": "high",
             "reason": "The candidate is less directly relevant."},
            {"recommendation": "human_review_required", "confidence": "medium",
             "reason": "The candidate remains ambiguous."},
        ]

        second = service.prepare_snapshot(
            self.package_id, force=True, reviewer="Reviewer"
        )["snapshot"]

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        saved = self.research.get(self.package_id)
        self.assertEqual(len(saved["supervised_autopilot_snapshots"]), 2)
        self.assertEqual(saved["supervised_autopilot_snapshots"][0]["status"],
                         "invalidated")
        self.assertEqual(saved["active_supervised_autopilot_snapshot_id"],
                         second["snapshot_id"])
        self.assertEqual(service.current_snapshot(self.package_id)["snapshot_id"],
                         second["snapshot_id"])

    def test_true_external_package_change_fails_stale_with_explanation(self):
        service, _ = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        package = self.research.get(self.package_id)
        package["candidate_sources"][0]["page_title"] = "Externally changed title"
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            SupervisedCampaignAutopilotError,
            "candidate evidence or package metadata changed.*Reanalyze Package",
        ):
            service.approve(
                self.package_id,
                {"KSC-AMBIG000001": "rejected"},
                expected_snapshot_id=preview["snapshot_id"],
                expected_preview_fingerprint=preview["review_fingerprint"],
                reviewer="Reviewer",
            )

    def test_package_approval_is_atomic_and_duplicate_is_write_free(self):
        service, _ = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        decisions = {
            "KSC-GOOD0000001": "selected",
            "KSC-AMBIG000001": "rejected",
            "KSC-BAD00000001": "rejected",
        }
        result = service.approve(
            self.package_id, decisions,
            expected_snapshot_id=preview["snapshot_id"],
            expected_preview_fingerprint=preview["preview_fingerprint"],
            reviewer="Reviewer", notes="Reviewed as one package.",
        )
        self.assertEqual(result["status"], "approved")
        saved = self.research.get(self.package_id)
        self.assertEqual(saved["selected_sources"], ["KSC-GOOD0000001"])
        self.assertEqual(saved["history"][-1]["event"],
                         "supervised_package_approved")
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        duplicate = service.approve(
            self.package_id, decisions,
            expected_snapshot_id=preview["snapshot_id"],
            expected_preview_fingerprint=preview["preview_fingerprint"],
            reviewer="Reviewer", notes="Reviewed as one package.",
        )
        self.assertEqual(duplicate["status"], "already_approved")
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_missing_explicit_human_decision_fails_closed(self):
        service, _ = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        with self.assertRaisesRegex(
            SupervisedCampaignAutopilotError, "require human judgment"
        ):
            service.approve(
                self.package_id,
                {},
                expected_snapshot_id=preview["snapshot_id"],
                expected_preview_fingerprint=preview["preview_fingerprint"],
                reviewer="Reviewer",
            )

    def test_configured_data_root_keeps_runtime_and_taxonomy_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(root)}):
                service = SupervisedCampaignAutopilotService()
            self.assertEqual(service.research.repository_root, root)
            self.assertEqual(
                service.research.planner.taxonomy_path.resolve(),
                (APPLICATION_ROOT / "app" / "data"
                 / "knowledge_coverage_taxonomy.json").resolve(),
            )

    def test_autopilot_review_get_is_read_only_and_has_campaign_return(self):
        service, provider = self.service()
        service.prepare_snapshot(self.package_id)
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.SupervisedCampaignAutopilotService", return_value=service
        ):
            client = flask_app.test_client()
            response = client.get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )
            repeated = client.get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review Source Package", response.data)
        self.assertIn(b"Return to Campaign Control Center", response.data)
        self.assertIn(b"AI curated: 2", response.data)
        self.assertIn(b"Human decisions needed: 1", response.data)
        self.assertIn(b"Approve AI-Reviewed Package", response.data)
        self.assertEqual(response.data, repeated.data)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_legacy_snapshot_without_equivalent_count_renders_write_free_and_ai_free(self):
        service, provider = self.service()
        service.prepare_snapshot(self.package_id)
        package = self.research.get(self.package_id)
        stored_review_fingerprint = package["supervised_autopilot_snapshots"][0][
            "review_fingerprint"
        ]
        for recommendation in package["supervised_autopilot_snapshots"][0]["recommendations"]:
            recommendation.pop("equivalent_candidate_count", None)
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        calls_before = provider.calls
        flask_app.config.update(TESTING=True)

        with patch(
            "app.app.SupervisedCampaignAutopilotService", return_value=service
        ):
            client = flask_app.test_client()
            response = client.get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )
            repeated = client.get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, repeated.data)
        self.assertNotIn(b"package entries resolve to this same canonical source", response.data)
        self.assertEqual(provider.calls, calls_before)
        self.assertEqual(
            service.current_snapshot(self.package_id)["review_fingerprint"],
            stored_review_fingerprint,
        )
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_live_shaped_twelve_candidate_package_renders_eleven_review_subjects(self):
        package = self.research.get(self.package_id)
        package["candidate_sources"].insert(
            1, deepcopy(package["candidate_sources"][0])
        )
        for index in range(8):
            package["candidate_sources"].append(self._candidate(
                f"KSC-REJECT{index:05d}", title=f"Deterministic mismatch {index}",
                topic_relevant=False,
            ))
        self.research._path(self.package_id).write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        service, provider = self.service()
        snapshot = service.prepare_snapshot(self.package_id)["snapshot"]
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        calls_before = provider.calls
        flask_app.config.update(TESTING=True)

        with patch(
            "app.app.SupervisedCampaignAutopilotService", return_value=service
        ):
            response = flask_app.test_client().get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )

        self.assertEqual(len(package["candidate_sources"]), 12)
        self.assertEqual(len(snapshot["recommendations"]), 11)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"2 package entries resolve to this same canonical source", response.data
        )
        self.assertEqual(provider.calls, calls_before)
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_explicit_reanalysis_post_creates_new_snapshot(self):
        service, provider = self.service()
        first = service.prepare_snapshot(self.package_id)["snapshot"]
        provider.results = [
            {"recommendation": "recommended_keep", "confidence": "high",
             "reason": "Directly addresses the governed storage gap."},
            {"recommendation": "human_review_required", "confidence": "medium",
             "reason": "The candidate remains ambiguous."},
        ]
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.SupervisedCampaignAutopilotService", return_value=service
        ):
            response = flask_app.test_client().post(
                f"/curator/growth/source-research/{self.package_id}/autopilot/reanalyze"
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"/curator/growth/source-research/{self.package_id}/autopilot",
            response.headers["Location"],
        )
        self.assertNotEqual(
            first["snapshot_id"],
            service.current_snapshot(self.package_id)["snapshot_id"],
        )

    def test_missing_snapshot_get_is_read_only_and_offers_explicit_analysis(self):
        service, provider = self.service()
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.SupervisedCampaignAutopilotService", return_value=service
        ):
            response = flask_app.test_client().get(
                f"/curator/growth/source-research/{self.package_id}/autopilot"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Analyze Package", response.data)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(before, {path: path.read_bytes()
                                  for path in self.root.rglob("*.json")})

    def test_package_post_redirects_to_campaign_and_triggers_bounded_progression(self):
        service, _ = self.service()
        preview = service.prepare_snapshot(self.package_id)["snapshot"]
        orchestration = Mock()
        orchestration.read_persisted.return_value = [{
            "campaign_id": self.campaign_id,
            "orchestration_id": "KORCH-AUTOPILOT1",
            "work_item_states": [{
                "work_item_id": self.work_id, "package_id": self.package_id,
                "state": "awaiting_human_review",
                "action_authority": "human_gate",
                "next_action": "approve_source",
            }],
        }]
        flask_app.config.update(TESTING=True)
        form = {
            "snapshot_id": preview["snapshot_id"],
            "preview_fingerprint": preview["preview_fingerprint"],
            "decision_KSC-GOOD0000001": "selected",
            "decision_KSC-AMBIG000001": "rejected",
            "decision_KSC-BAD00000001": "rejected",
        }
        with (
            patch("app.app.SupervisedCampaignAutopilotService", return_value=service),
            patch("app.app.KnowledgeCampaignOrchestrationService", orchestration),
        ):
            response = flask_app.test_client().post(
                f"/curator/growth/source-research/{self.package_id}/autopilot/approve",
                data=form,
            )
            before_duplicate = {
                path: path.read_bytes() for path in self.root.rglob("*.json")
            }
            duplicate = flask_app.test_client().post(
                f"/curator/growth/source-research/{self.package_id}/autopilot/approve",
                data=form,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(duplicate.status_code, 302)
        self.assertIn(
            f"/curator/growth/coverage-campaigns/{self.campaign_id}/orchestration",
            response.headers["Location"],
        )
        orchestration.return_value.continue_after_human_gate.assert_called_once_with(
            "KORCH-AUTOPILOT1", actor="Supervised Campaign Autopilot",
            max_transitions=3,
        )
        self.assertEqual(before_duplicate, {
            path: path.read_bytes() for path in self.root.rglob("*.json")
        })


if __name__ == "__main__":
    unittest.main()
