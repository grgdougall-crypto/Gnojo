import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.services.knowledge_workflow_generation_service import (
    KnowledgeWorkflowGenerationError,
    KnowledgeWorkflowGenerationService,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerService,
)
from app.services.authentication_service import AuthenticationService


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
        claims = self._claims()
        self._write(self.campaign_root / "research/KSR-WORKFLOW01.json", {
            "package_id": "KSR-WORKFLOW01", "campaign_id": self.campaign_id,
            "work_item_id": self.work_id, "status": "approved",
        })
        self._write(self.campaign_root / "evidence_extraction/KEX-WORKFLOW01.json", {
            "extraction_id": "KEX-WORKFLOW01", "research_package_id": "KSR-WORKFLOW01",
            "status": "approved", "evidence_units": [
                {"evidence_id": evidence_id, "review_state": "approved"}
                for claim in claims for evidence_id in claim["evidence_ids"]
            ],
        })
        self._write_claim_plan(claims)
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
        evidence_ids = sorted({
            evidence_id for claim in claims if claim.get("review_state") == "approved"
            for evidence_id in claim.get("evidence_ids", [])
        })
        self._write(self.campaign_root / "claim_planning/KCPM-WORKFLOW01.json", {
            "schema_version": "1.0", "claim_plan_id": "KCPM-WORKFLOW01",
            "campaign_id": self.campaign_id, "work_item_id": self.work_id,
            "target_asset_type": "workflow", "status": status,
            "approved_evidence_ids": evidence_ids,
            "claims": claims,
        })
        extraction_path = self.campaign_root / "evidence_extraction/KEX-WORKFLOW01.json"
        if extraction_path.parent.exists():
            self._write(extraction_path, {
                "extraction_id": "KEX-WORKFLOW01", "research_package_id": "KSR-WORKFLOW01",
                "status": "approved", "evidence_units": [
                    {"evidence_id": evidence_id, "review_state": "approved"}
                    for evidence_id in evidence_ids
                ],
            })

    def _planned(self):
        package = self.service.prepare(self.campaign_id, self.work_id)
        return self.service.plan(package["generation_id"])

    def _draft(self):
        package = self._planned()
        return self.service.prepare_draft(package["generation_id"])

    def _capability_service(self):
        identity = "capability:desktop-support:windows:vpn:missing_workflow"
        taxonomy_path = self.root / "taxonomy.json"
        taxonomy_path.write_text(json.dumps({
            "schema_version": "1.0", "domains": [{
                "id": "desktop-support", "title": "Desktop Support",
                "category": "Desktop Support", "platforms": ["Windows"],
                "capability_catalog": "capabilities.json", "areas": [],
            }],
        }), encoding="utf-8")
        (self.root / "capabilities.json").write_text(json.dumps({
            "schema_version": "1.0", "catalog_id": "test-capabilities",
            "domain_id": "desktop-support", "title": "Test capabilities",
            "capabilities": [{
                "id": "vpn", "title": "VPN", "level": "core",
                "platform": "Windows", "category": "Network access",
                "terms": ["vpn", "virtual private network"],
                "expected_artifacts": ["workflow", "article"],
                "likely_relationships": ["workflow_article"],
                "artifact_matches": {"workflow": [], "article": [], "command": []},
            }],
        }), encoding="utf-8")
        self.campaign.update({
            "domain": "desktop-support",
            "creation_metadata": {
                "initiated_by": "autonomous_growth_stage2",
                "gap_identity": identity,
                "selected_gap": {
                    "gap_type": "missing_workflow", "gap_identity": identity,
                    "domain_id": "desktop-support", "area_id": "vpn",
                    "capability_id": "vpn", "platform": "Windows",
                    "expected_artifacts": ["workflow", "article"],
                },
            },
            "gaps": [{
                "gap_id": "KCG-WORKFLOW01", "gap_type": "missing_workflow",
                "gap_identity": identity, "area_id": "vpn", "capability_id": "vpn",
                "platform": "Windows", "expected_artifacts": ["workflow", "article"],
            }],
        })
        self.campaign["work_items"][0].update({
            "area_id": "vpn", "target_asset": "vpn", "gap_identity": identity,
            "capability_id": "vpn", "platform": "Windows",
            "expected_artifacts": ["workflow", "article"],
        })
        self._write(self.campaign_root / f"{self.campaign_id}.json", self.campaign)
        claims = self._claims()
        claims[0]["workflow_spec"]["workflow_name"] = "VPN"
        self._write_claim_plan(claims)
        planner = KnowledgeCoveragePlannerService(
            self.root, self.campaign_root, taxonomy_path
        )
        service = KnowledgeWorkflowGenerationService(
            self.root, self.campaign_root, self.root / "app/workflow_drafts",
            planner=planner,
        )
        package = service.plan(service.prepare(self.campaign_id, self.work_id)["generation_id"])
        return service, package

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

    def test_capability_proposal_is_deterministic_evidence_backed_and_read_only(self):
        service, package = self._capability_service()
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}

        first = service.proposal(package["generation_id"])
        second = service.proposal(package["generation_id"])

        self.assertEqual(first, second)
        self.assertTrue(first["eligible"])
        self.assertEqual(first["capability"]["gap_identity"],
                         "capability:desktop-support:windows:vpn:missing_workflow")
        self.assertEqual(first["workflow"]["id"], "vpn")
        self.assertEqual(first["workflow"]["start_node"], "q_start")
        self.assertEqual(first["workflow"]["node_count"], 6)
        self.assertTrue(first["questions"])
        self.assertTrue(first["instructions"])
        self.assertTrue(first["outcomes"])
        self.assertEqual(first["evidence"]["source_urls"],
                         ["https://learn.microsoft.com/windows"])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})

    def test_capability_approval_creates_one_unpublished_draft_and_is_idempotent(self):
        service, package = self._capability_service()
        proposal = service.proposal(package["generation_id"])

        first = service.approve_draft_creation(
            package["generation_id"], reviewer="Greg Dougall",
            expected_proposal_fingerprint=proposal["proposal_fingerprint"],
            notes="Evidence and safety boundaries reviewed.",
        )
        draft_path = self.root / "app/workflow_drafts/vpn.json"
        draft_before = draft_path.read_bytes()
        package_before = (service.package_root / f"{package['generation_id']}.json").read_bytes()
        second = service.approve_draft_creation(
            package["generation_id"], reviewer="Greg Dougall",
            expected_proposal_fingerprint=proposal["proposal_fingerprint"],
            notes="Evidence and safety boundaries reviewed.",
        )

        self.assertEqual(first, second)
        self.assertEqual(draft_path.read_bytes(), draft_before)
        self.assertEqual((service.package_root / f"{package['generation_id']}.json").read_bytes(),
                         package_before)
        self.assertEqual(first["draft_creation_review"]["reviewer"], "Greg Dougall")
        self.assertEqual(first["draft_creation_review"]["campaign_id"], self.campaign_id)
        self.assertEqual(first["status"], "handed_off")
        saved = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["knowledge_factory"]["capability_id"], "vpn")
        self.assertEqual(
            saved["knowledge_factory"]["gap_identity"],
            "capability:desktop-support:windows:vpn:missing_workflow",
        )
        self.assertEqual(list((self.root / "app/workflow_publications").rglob("*.json")), [])

    def test_capability_proposal_route_is_read_only_and_approval_uses_existing_orchestration(self):
        service, package = self._capability_service()
        proposal = service.proposal(package["generation_id"])
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        orchestration = Mock()
        orchestration.campaign_root = self.campaign_root
        orchestration.read_persisted.return_value = [{
            "campaign_id": self.campaign_id, "orchestration_id": "KORCH-TEST",
        }]
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)

        with patch("app.app.KnowledgeWorkflowGenerationService", return_value=service), \
             patch("app.app.KnowledgeCampaignOrchestrationService",
                   return_value=orchestration):
            client = flask_app.test_client()
            page = client.get(
                f"/curator/growth/workflow-generation/{package['generation_id']}"
            )
            self.assertEqual(before, {
                path: path.read_bytes() for path in self.root.rglob("*.json")
            })
            response = client.post(
                f"/curator/growth/workflow-generation/{package['generation_id']}"
                "/approve-draft-creation",
                data={"proposal_fingerprint": proposal["proposal_fingerprint"],
                      "notes": "Reviewed."},
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Missing Workflow Proposal", page.data)
        self.assertIn(b"Approve Draft Creation", page.data)
        self.assertIn(b"VPN", page.data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue((self.root / "app/workflow_drafts/vpn.json").is_file())
        orchestration.refresh.assert_called_once_with("KORCH-TEST")
        self.assertEqual(list((self.root / "app/workflow_publications").rglob("*.json")), [])

    def test_draft_creation_approval_route_requires_reviewer_and_csrf(self):
        prior = {key: flask_app.config.get(key) for key in (
            "TESTING", "AUTH_TEST_BYPASS", "GNOJO_STABLE_SESSION_SECRET_CONFIGURED",
            "GNOJO_REVIEWER_USERNAME", "GNOJO_REVIEWER_PASSWORD_HASH",
        )}
        flask_app.config.update(
            TESTING=True, AUTH_TEST_BYPASS=False,
            GNOJO_STABLE_SESSION_SECRET_CONFIGURED=True,
            GNOJO_REVIEWER_USERNAME="Reviewer",
            GNOJO_REVIEWER_PASSWORD_HASH="configured-test-hash",
        )
        try:
            client = flask_app.test_client()
            unauthenticated = client.post(
                "/curator/growth/workflow-generation/KWG-TEST/approve-draft-creation"
            )
            with client.session_transaction() as reviewer_session:
                reviewer_session[AuthenticationService.AUTHENTICATED_KEY] = True
                reviewer_session[AuthenticationService.USERNAME_KEY] = "Reviewer"
                reviewer_session[AuthenticationService.ROLE_KEY] = "reviewer_admin"
                reviewer_session[AuthenticationService.CSRF_KEY] = "valid-token"
            missing_csrf = client.post(
                "/curator/growth/workflow-generation/KWG-TEST/approve-draft-creation"
            )
            invalid_csrf = client.post(
                "/curator/growth/workflow-generation/KWG-TEST/approve-draft-creation",
                data={"authenticity_token": "wrong-token"},
            )
        finally:
            flask_app.config.update(prior)
        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(invalid_csrf.status_code, 400)

    def test_capability_proposal_stale_fingerprint_fails_closed(self):
        service, package = self._capability_service()
        proposal = service.proposal(package["generation_id"])
        claim_path = self.campaign_root / "claim_planning/KCPM-WORKFLOW01.json"
        claims = json.loads(claim_path.read_text(encoding="utf-8"))
        claims["claims"][0]["workflow_spec"]["fields"]["help_text"] += " Changed."
        self._write(claim_path, claims)
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "changed"):
            service.approve_draft_creation(
                package["generation_id"], reviewer="Reviewer",
                expected_proposal_fingerprint=proposal["proposal_fingerprint"],
            )

    def test_capability_proposal_identity_collision_fails_closed(self):
        service, package = self._capability_service()
        self._write(self.root / "app/workflow_drafts/vpn.json", {
            "workflow_id": "vpn", "name": "Collision", "start_node": "done",
            "nodes": {"done": {"type": "resolution", "message": "Existing."}},
        })
        proposal = service.proposal(package["generation_id"])
        self.assertFalse(proposal["eligible"])
        self.assertTrue(any("proposed identity" in item or "proposed filename" in item
                            for item in proposal["blockers"]))

    def test_capability_proposal_unsafe_state_change_fails_closed(self):
        service, package = self._capability_service()
        path = service.package_root / f"{package['generation_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["workflow_plan"]["nodes"][1]["fields"].update({
            "title": "Reset the VPN", "instruction": "Reset the VPN now."
        })
        self._write(path, raw)
        unsafe = service.proposal(package["generation_id"])
        self.assertFalse(unsafe["eligible"])
        self.assertTrue(any("state-changing safety" in item
                            for item in unsafe["blockers"]))

    def test_state_change_matching_requires_exact_action_context(self):
        matcher = KnowledgeWorkflowGenerationService._state_change_signals
        for text in (
            "Multiple profiles are configured.",
            "The configured profile is already available.",
            "The restart status is pending.",
            "The service restart is scheduled.",
            "The updater reports the current version.",
        ):
            with self.subTest(false_positive=text):
                self.assertEqual(matcher(text), [])
        expected = {
            "Configure the VPN profile.": ["configure"],
            "Change the network setting.": ["change"],
            "Delete the obsolete profile.": ["delete"],
            "Restart the service.": ["restart"],
            "Save work, then reset the adapter.": ["reset"],
            "Use Settings to disable the adapter.": ["disable"],
        }
        for text, signals in expected.items():
            with self.subTest(true_positive=text):
                self.assertEqual(matcher(text), signals)

    def test_configured_condition_does_not_block_capability_proposal(self):
        service, package = self._capability_service()
        path = service.package_root / f"{package['generation_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        target = raw["workflow_plan"]["nodes"][1]
        target["fields"].update({
            "title": "Select the VPN",
            "instruction": (
                "If multiple VPN profiles are configured, select the intended "
                "connection and then select Connect."
            ),
        })
        self._write(path, raw)
        proposal = service.proposal(package["generation_id"])
        self.assertEqual(
            service.safety_exception_review(package["generation_id"])["exceptions"],
            [],
        )
        self.assertTrue(proposal["validation"]["valid"])
        self.assertTrue(proposal["eligible"])

    def test_governed_safety_review_updates_one_plan_node_and_revalidates(self):
        service, package = self._capability_service()
        path = service.package_root / f"{package['generation_id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        target = raw["workflow_plan"]["nodes"][1]
        target["fields"].update({
            "title": "Connect to the VPN",
            "instruction": "Configure the intended VPN connection, then select Connect.",
        })
        self._write(path, raw)
        review = service.safety_exception_review(
            package["generation_id"], target["node_id"]
        )
        exception = review["current"]
        self.assertEqual(
            exception["instruction"],
            "Configure the intended VPN connection, then select Connect.",
        )
        self.assertTrue(exception["claim_ids"])
        self.assertTrue(exception["evidence_ids"])
        with self.assertRaisesRegex(KnowledgeWorkflowGenerationError, "changed"):
            service.review_safety_exception(
                package["generation_id"], target["node_id"],
                "accept_recommended", reviewer="Reviewer",
                expected_proposal_fingerprint="stale",
                expected_exception_fingerprint=exception["exception_fingerprint"],
            )
        service.review_safety_exception(
            package["generation_id"], target["node_id"],
            "accept_recommended", reviewer="Reviewer",
            expected_proposal_fingerprint=review["proposal_fingerprint"],
            expected_exception_fingerprint=exception["exception_fingerprint"],
            notes="Proportional guidance reviewed.",
        )
        updated = service.get(package["generation_id"])
        updated_target = next(
            item for item in updated["workflow_plan"]["nodes"]
            if item["node_id"] == target["node_id"]
        )
        self.assertIn("authorized", updated_target["fields"]["help_text"])
        self.assertEqual(updated["safety_reviews"][-1]["reviewer"], "Reviewer")
        self.assertEqual(updated["safety_reviews"][-1]["claim_ids"], exception["claim_ids"])
        self.assertEqual(service.safety_exception_review(
            package["generation_id"]
        )["exceptions"], [])
        self.assertTrue(service.proposal(package["generation_id"])["eligible"])
        self.assertEqual(list((self.root / "app/workflow_publications").rglob("*.json")), [])

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
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
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
