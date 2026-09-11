import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.services.knowledge_builder_service import (
    KnowledgeBuilderError,
    KnowledgeBuilderService,
)
from app.services.authentication_service import ReviewerAccessPolicy
from app.services.knowledge_workflow_generation_service import (
    KnowledgeWorkflowGenerationError,
)


class _Planner:
    def __init__(self, campaign):
        self.campaign = campaign

    def get(self, campaign_id):
        assert campaign_id == self.campaign["campaign_id"]
        return self.campaign


class _Workflows:
    def __init__(self):
        self.items = []
        self.drafts = Mock()
        self.approvals = []
        self.unsafe = False
        self.safety_decisions = []

    def list_for_campaign(self, campaign_id):
        return list(self.items)

    def proposal(self, generation_id):
        result = {
            "supported": True, "eligible": not self.unsafe,
            "proposal_fingerprint": "proposal-fingerprint",
            "workflow": {"id": "vpn", "name": "VPN", "platform": "Windows",
                         "node_count": 2},
            "questions": [],
            "instructions": [{"title": "Connect", "instruction": "Select Connect."}],
            "outcomes": [{"title": "VPN Connected"}],
            "blockers": (["The proposed workflow does not pass deterministic validation.",
                           "Instruction i_unsafe has unresolved state-changing safety guidance."]
                          if self.unsafe else []),
            "validation": {"valid": not self.unsafe},
        }
        return result

    def safety_exception_review(self, generation_id, node_id=None):
        exceptions = []
        if self.unsafe:
            exceptions = [{
                "node_id": "i_unsafe", "title": "Connect",
                "instruction": "Select the intended VPN and choose Connect.",
                "classification": "state_changing",
                "state_change_signals": ["configure"],
                "reason": "Connecting changes device network state and needs proportional guidance.",
                "recommended_guidance": "Confirm authorization before connecting.",
                "claims": [{
                    "normalized_claim": "Select the intended VPN and choose Connect.",
                    "provenance": [{"source_title": "Connect to a VPN in Windows",
                                    "publisher": "Microsoft"}],
                }],
                "claim_ids": ["CLM-SAFE"], "evidence_ids": ["EVD-SAFE"],
                "source_urls": ["https://support.example/vpn"],
                "exception_fingerprint": "exception-fingerprint",
            }]
        if node_id is not None:
            exceptions = [item for item in exceptions if item["node_id"] == node_id]
            if len(exceptions) != 1:
                raise KnowledgeWorkflowGenerationError("no longer current")
        return {
            "generation_id": generation_id, "campaign_id": "KCAMP-TEST",
            "work_item_id": "KCW-VPN", "workflow": {"name": "VPN"},
            "proposal_fingerprint": "proposal-fingerprint",
            "exceptions": exceptions,
            "current": exceptions[0] if node_id is not None else None,
            "read_only": True,
        }

    def review_safety_exception(self, generation_id, node_id, decision, **values):
        self.safety_decisions.append((generation_id, node_id, decision, values))
        if decision == "accept_recommended":
            self.unsafe = False
        return self.items[0]

    def approve_draft_creation(self, generation_id, **values):
        self.approvals.append((generation_id, values))
        self.items[0].update(
            status="handed_off", content_studio_filename="vpn.json",
            workflow_draft={"workflow_id": "vpn"},
        )
        return self.items[0]


class _Orchestration:
    def __init__(self, root, workflows):
        self.repository_root = root
        self.campaign_root = root / "knowledge_campaigns"
        self.campaign_root.mkdir(parents=True)
        self.campaign = {
            "campaign_id": "KCAMP-TEST",
            "work_items": [{"work_item_id": "KCW-VPN", "work_type": "workflow",
                            "target_asset": "vpn"}],
        }
        self.planner = _Planner(self.campaign)
        self.workflows = workflows
        self.record = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "mode": "supervised", "work_item_states": [{
                "work_item_id": "KCW-VPN", "work_type": "workflow",
                "title": "VPN", "state": "active",
                "next_action": "prepare_workflow_package",
                "action_authority": "machine_safe", "review_link": None,
            }],
        }
        self.transitions = []

    def read_persisted(self, campaign_root):
        return [self.record]

    def refresh(self, orchestration_id):
        return self.record

    def advance_item(self, orchestration_id, work_item_id, actor="Human"):
        self.transitions.append((work_item_id, actor))
        state = self.record["work_item_states"][0]
        if not self.workflows.items:
            self.workflows.items.append({
                "generation_id": "KWG-VPN", "campaign_id": "KCAMP-TEST",
                "work_item_id": "KCW-VPN", "proposed_workflow_id": "vpn",
                "status": "prepared", "workflow_plan": None,
                "approved_claim_ids": ["CLM-1"],
                "approved_evidence_ids": ["EVD-1"],
            })
            state.update(next_action="plan_workflow", action_authority="machine_safe")
        else:
            self.workflows.items[0].update(
                status="plan_ready", workflow_plan={
                    "nodes": [{"source_urls": ["https://example.test/vpn"]}]
                },
            )
            state.update(state="awaiting_human_review",
                         next_action="approve_workflow_draft_creation",
                         action_authority="human_gate", review_link="/technical-review")
        return {**self.record, "execution": {"outcomes": [{"status": "completed"}]}}


class _Drafts:
    def __init__(self):
        self.value = {"workflow_id": "vpn", "name": "VPN", "start_node": "done",
                      "nodes": {"done": {"type": "resolution", "title": "Connected",
                                         "message": "VPN is connected."}}}

    def get_draft(self, filename):
        return self.value if filename == "vpn.json" else None


class _Publications:
    def __init__(self):
        self.published = False
        self.calls = 0

    @staticmethod
    def content_hash(workflow):
        return hashlib.sha256(json.dumps(workflow, sort_keys=True).encode()).hexdigest()

    def status(self, workflow_id):
        versions = ([{"version": 1, "content_hash": self.content_hash(_Drafts().value)}]
                    if self.published else [])
        return {"is_published": self.published,
                "current_version": 1 if self.published else None, "versions": versions}

    def publish(self, workflow, source_filename):
        self.calls += 1
        self.published = True
        return self.status(workflow["workflow_id"])


class _Evidence:
    def __init__(self):
        self.role_calls = []
        self.review_calls = []
        self.confirm_calls = []
        self.units = [self._unit("EVD-EXCEPTION-1", "VPN sign-in may be required.")]

    @staticmethod
    def _unit(evidence_id, claim):
        return {
            "evidence_id": evidence_id,
            "normalized_claim": claim,
            "supporting_passage": claim,
            "source_title": "Connect to a VPN in Windows",
            "source_location": {"heading": "Connect to a VPN"},
            "review_state": "proposed",
            "candidacy_role": None,
            "candidacy": {
                "human_confirmed_role": None,
                "machine_rationale": "The role depends on workflow applicability.",
            },
            "workflow_evidence_compression": {
                "decision": "human_exception",
                "reason": "Platform applicability requires human judgment.",
            },
        }

    def review_workspace(self, extraction_id):
        assert extraction_id == "KEX-VPN"
        unresolved = [unit for unit in self.units if not unit.get("settled")]
        return {
            "package": {
                "extraction_id": extraction_id,
                "campaign_id": "KCAMP-TEST",
                "work_item_id": "KCW-VPN",
                "source_title": "VPN",
                "publisher": "Microsoft Support",
                "canonical_source_url": "https://support.microsoft.com/windows/vpn",
            },
            "context": {"campaign_title": "VPN", "facet": "vpn_connectivity"},
            "compression": {"enabled": True, "exception_units": unresolved},
            "candidate_set_current": True,
            "candidacy_ready_to_confirm": all(
                (unit.get("candidacy") or {}).get("human_confirmed_role")
                for unit in self.units
            ),
        }

    def set_candidacy_role(self, extraction_id, evidence_id, role):
        self.role_calls.append((extraction_id, evidence_id, role))
        unit = next(item for item in self.units if item["evidence_id"] == evidence_id)
        unit["candidacy"]["human_confirmed_role"] = role
        unit["candidacy_role"] = role
        if role == "context":
            unit["settled"] = True

    def confirm_candidate_set(self, extraction_id):
        self.confirm_calls.append(extraction_id)

    def review_evidence(self, extraction_id, evidence_id, decision, notes):
        self.review_calls.append((extraction_id, evidence_id, decision, notes))
        next(item for item in self.units
             if item["evidence_id"] == evidence_id)["settled"] = True


class KnowledgeBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workflows = _Workflows()
        self.orchestration = _Orchestration(self.root, self.workflows)
        self.drafts = _Drafts()
        self.publications = _Publications()
        self.evidence = _Evidence()
        self.orchestration.evidence = self.evidence
        lifecycle = SimpleNamespace(
            reasoning_review_error="", reasoning_reviews=(),
            validation=SimpleNamespace(reasoning_findings=()),
        )
        self.service = KnowledgeBuilderService(
            self.orchestration, self.workflows, self.drafts, self.publications,
            lifecycle_factory=lambda root: SimpleNamespace(project=lambda workflow_id: lifecycle),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_three_step_projection_and_safe_automatic_continuation(self):
        initial = self.service.project("KCAMP-TEST", "KCW-VPN")
        self.assertEqual(initial["step"], "prepare")
        prepared = self.service.prepare("KCAMP-TEST", "KCW-VPN")
        self.assertEqual(prepared["step"], "review")
        self.assertEqual(prepared["execution"]["transitions"], 2)
        self.assertEqual(len(self.orchestration.transitions), 2)
        self.assertEqual(prepared["counts"], {"sources": 1, "evidence": 1, "claims": 1})

    def test_builder_excludes_legacy_safety_review_work_from_workflow_items(self):
        self.orchestration.campaign["work_items"].append({
            "work_item_id": "KCW-LEGACY-SAFETY", "work_type": "safety_review",
            "title": "VPN", "area_id": "vpn",
        })
        self.orchestration.record["work_item_states"].append({
            "work_item_id": "KCW-LEGACY-SAFETY", "work_type": "safety_review",
            "title": "VPN", "state": "awaiting_human_review",
            "next_action": "review_evidence", "action_authority": "human_gate",
            "package_id": "KEX-LEGACY", "review_link": "/legacy-evidence",
        })
        items = self.service.index()
        self.assertEqual([item["work_item_id"] for item in items], ["KCW-VPN"])
        with self.assertRaisesRegex(KnowledgeBuilderError, "missing or ambiguous"):
            self.service.project("KCAMP-TEST", "KCW-LEGACY-SAFETY")

    def test_exception_stops_safe_processing(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", review_link="/evidence/KEX-1",
                     package_id="KEX-VPN")
        result = self.service.prepare("KCAMP-TEST", "KCW-VPN")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(
            result["attention"]["url"],
            "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
            "exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1",
        )
        self.assertEqual(result["attention"]["count"], 1)
        self.assertEqual(result["execution"]["transitions"], 0)

    def test_multiple_exceptions_use_compact_list_and_do_not_render_unrelated_units(self):
        self.evidence.units.append(
            self.evidence._unit("EVD-EXCEPTION-2", "Choose the intended VPN connection.")
        )
        self.evidence.units.append({
            **self.evidence._unit("EVD-SETTLED", "Unrelated settled evidence."),
            "settled": True,
        })
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN")
        projection = self.service.project("KCAMP-TEST", "KCW-VPN")
        self.assertTrue(projection["attention"]["url"].endswith("/exceptions/evidence"))
        facade = Mock()
        facade.evidence_exceptions.return_value = self.service.evidence_exceptions(
            "KCAMP-TEST", "KCW-VPN"
        )
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().get(projection["attention"]["url"])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 evidence exceptions need review", response.data)
        self.assertNotIn(b"Unrelated settled evidence", response.data)
        facade.decide_evidence.assert_not_called()
        facade.decide_evidence_role.assert_not_called()

    def test_focused_exception_get_is_read_only_and_keeps_legacy_workspace_secondary(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN")
        workspace = self.service.evidence_exceptions(
            "KCAMP-TEST", "KCW-VPN", "EVD-EXCEPTION-1"
        )
        facade = Mock()
        facade.evidence_exceptions.return_value = workspace
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        path = ("/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
                "exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1")
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Should this proposition support the workflow", response.data)
        self.assertIn(b"Machine recommendation", response.data)
        self.assertIn(b"support.microsoft.com", response.data)
        self.assertIn(b"Return to Knowledge Builder", response.data)
        self.assertIn(b"Open full evidence review", response.data)
        for legacy_control in (
            b"Apply filters", b"Clear filters", b"Candidate Sources",
            b"Confirm Candidate Set", b"Assign Visible Context",
            b"Refresh Candidate", b"Re-extract", b"Extraction method",
            b"Review progress", b"View settled propositions",
        ):
            with self.subTest(legacy_control=legacy_control):
                self.assertNotIn(legacy_control, response.data)
        facade.decide_evidence.assert_not_called()
        facade.decide_evidence_role.assert_not_called()

    def test_builder_card_cannot_render_legacy_evidence_link_as_primary_action(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN",
                     review_link="/curator/growth/evidence-extraction/KEX-VPN")
        projection = self.service.project("KCAMP-TEST", "KCW-VPN")
        projection["attention"]["url"] = "/curator/growth/evidence-extraction/KEX-VPN"
        facade = Mock()
        facade.project.return_value = projection
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().get(
                "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'href="/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/'
            b'exceptions/evidence"', response.data,
        )
        self.assertNotIn(
            b'href="/curator/growth/evidence-extraction/KEX-VPN"', response.data,
        )

    def test_single_exception_list_routes_directly_to_exact_exception(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN")
        workspace = self.service.evidence_exceptions("KCAMP-TEST", "KCW-VPN")
        facade = Mock()
        facade.evidence_exceptions.return_value = workspace
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        path = ("/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
                "exceptions/evidence")
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().get(path)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(
            "/exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1"
        ))

    def test_actual_builder_http_chain_finishes_on_focused_exception_template(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN",
                     review_link="/curator/growth/evidence-extraction/KEX-VPN")
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        builder_path = "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN"
        client = flask_app.test_client()
        with patch("app.app.KnowledgeBuilderService", return_value=self.service):
            builder = client.get(builder_path)
            self.assertEqual(builder.status_code, 200)
            match = __import__("re").search(
                rb'<a[^>]+href="([^"]+)"[^>]*>Review exception</a>',
                builder.data,
            )
            self.assertIsNotNone(match)
            emitted_href = match.group(1).decode("utf-8")
            self.assertEqual(
                emitted_href,
                builder_path + "/exceptions/evidence",
            )
            focused = client.get(emitted_href, follow_redirects=True)
        self.assertEqual(focused.status_code, 200)
        self.assertEqual(
            focused.request.path,
            builder_path + "/exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1",
        )
        self.assertIn(b"Gnojo could not safely determine", focused.data)
        self.assertNotIn(b"Confirm the Evidence Candidate Set", focused.data)
        self.assertNotIn(b"Apply filters", focused.data)
        self.assertNotIn(b"Run Extraction Again", focused.data)

    def test_focused_post_returns_to_builder_after_authoritative_decision(self):
        facade = Mock()
        facade.decide_evidence_role.return_value = {
            "extraction_id": "KEX-VPN", "exceptions": [],
        }
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        path = ("/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
                "exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1/role")
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().post(path, data={"role": "context"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN",
            response.headers["Location"],
        )
        facade.decide_evidence_role.assert_called_once_with(
            "KCAMP-TEST", "KCW-VPN", "KEX-VPN", "EVD-EXCEPTION-1", "context"
        )

    def test_focused_role_decision_uses_authoritative_service_and_returns_to_builder(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN")
        result = self.service.decide_evidence_role(
            "KCAMP-TEST", "KCW-VPN", "KEX-VPN", "EVD-EXCEPTION-1", "context"
        )
        self.assertEqual(
            self.evidence.role_calls,
            [("KEX-VPN", "EVD-EXCEPTION-1", "context")],
        )
        self.assertEqual(result["exceptions"], [])

    def test_focused_evidence_decision_uses_authoritative_service(self):
        state = self.orchestration.record["work_item_states"][0]
        state.update(state="awaiting_human_review", next_action="review_evidence",
                     action_authority="human_gate", package_id="KEX-VPN")
        unit = self.evidence.units[0]
        unit["candidacy"]["human_confirmed_role"] = "candidate"
        unit["candidacy_role"] = "candidate"
        result = self.service.decide_evidence(
            "KCAMP-TEST", "KCW-VPN", "KEX-VPN", "EVD-EXCEPTION-1",
            "approved", "Supported by the source.",
        )
        self.assertEqual(
            self.evidence.review_calls,
            [("KEX-VPN", "EVD-EXCEPTION-1", "approved",
              "Supported by the source.")],
        )
        self.assertEqual(result["exceptions"], [])

    def test_exception_routes_retain_reviewer_auth_and_csrf_policy(self):
        get_path = ("/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
                    "exceptions/evidence/KEX-VPN/EVD-EXCEPTION-1")
        post_path = get_path + "/role"
        self.assertTrue(ReviewerAccessPolicy.requires_reviewer(get_path, "GET"))
        self.assertTrue(ReviewerAccessPolicy.requires_reviewer(post_path, "POST"))
        self.assertTrue(ReviewerAccessPolicy.requires_csrf(post_path, "POST"))
        self.assertFalse(ReviewerAccessPolicy.requires_csrf(get_path, "GET"))

    def test_workflow_approval_is_explicit_and_stale_safe(self):
        self.service.prepare("KCAMP-TEST", "KCW-VPN")
        with self.assertRaisesRegex(KnowledgeBuilderError, "changed"):
            self.service.approve_workflow(
                "KCAMP-TEST", "KCW-VPN", reviewer="Reviewer",
                proposal_fingerprint="stale",
            )
        complete = self.service.approve_workflow(
            "KCAMP-TEST", "KCW-VPN", reviewer="Reviewer",
            proposal_fingerprint="proposal-fingerprint",
        )
        self.assertEqual(complete["step"], "complete")
        self.assertFalse(complete["published"])
        self.assertEqual(len(self.workflows.approvals), 1)

    def test_review_safety_issue_http_chain_resolves_and_returns_to_builder(self):
        self.workflows.unsafe = True
        self.service.prepare("KCAMP-TEST", "KCW-VPN")
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        builder_path = "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN"
        client = flask_app.test_client()
        with patch("app.app.KnowledgeBuilderService", return_value=self.service):
            builder = client.get(builder_path)
            self.assertEqual(builder.status_code, 200)
            self.assertIn(b"Review safety issue", builder.data)
            self.assertNotIn(b"Instruction i_unsafe has unresolved", builder.data)
            link = __import__("re").search(
                rb'<a[^>]+href="([^"]+)"[^>]*>Review safety issue</a>',
                builder.data,
            ).group(1).decode("utf-8")
            focused = client.get(link, follow_redirects=True)
            self.assertEqual(focused.status_code, 200)
            self.assertEqual(
                focused.request.path,
                builder_path + "/exceptions/safety/KWG-VPN/i_unsafe",
            )
            self.assertIn(
                b"Select the intended VPN and choose Connect.", focused.data
            )
            self.assertIn(b"Accept recommended safety guidance", focused.data)
            self.assertNotIn(b"Create Workflow Plan", focused.data)
            self.assertNotIn(b"Draft Route Preview", focused.data)
            self.assertNotIn(b"Package History", focused.data)
            decision = client.post(
                focused.request.path + "/decision",
                data={
                    "decision": "accept_recommended",
                    "proposal_fingerprint": "proposal-fingerprint",
                    "exception_fingerprint": "exception-fingerprint",
                },
                follow_redirects=True,
            )
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.request.path, builder_path)
        self.assertIn(b"Deterministic validation now passes", decision.data)
        self.assertIn(b"Approve Workflow", decision.data)
        self.assertEqual(len(self.workflows.safety_decisions), 1)
        self.assertEqual(self.publications.calls, 0)

    def test_safety_exception_get_is_read_only_and_routes_are_auth_csrf_protected(self):
        self.workflows.unsafe = True
        self.service.prepare("KCAMP-TEST", "KCW-VPN")
        before = deepcopy(self.workflows.items)
        review = self.service.safety_exceptions("KCAMP-TEST", "KCW-VPN")
        self.assertEqual(len(review["exceptions"]), 1)
        self.assertEqual(self.workflows.items, before)
        get_path = ("/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN/"
                    "exceptions/safety/KWG-VPN/i_unsafe")
        post_path = get_path + "/decision"
        self.assertTrue(ReviewerAccessPolicy.requires_reviewer(get_path, "GET"))
        self.assertTrue(ReviewerAccessPolicy.requires_reviewer(post_path, "POST"))
        self.assertTrue(ReviewerAccessPolicy.requires_csrf(post_path, "POST"))
        self.assertFalse(ReviewerAccessPolicy.requires_csrf(get_path, "GET"))

    def test_publication_remains_a_separate_explicit_stale_safe_action(self):
        self.service.prepare("KCAMP-TEST", "KCW-VPN")
        complete = self.service.approve_workflow(
            "KCAMP-TEST", "KCW-VPN", reviewer="Reviewer",
            proposal_fingerprint="proposal-fingerprint",
        )
        with self.assertRaisesRegex(KnowledgeBuilderError, "changed"):
            self.service.publish("KCAMP-TEST", "KCW-VPN",
                                 expected_draft_fingerprint="stale")
        published = self.service.publish(
            "KCAMP-TEST", "KCW-VPN",
            expected_draft_fingerprint=complete["draft_fingerprint"],
        )
        self.assertTrue(published["published"])
        self.assertEqual(self.publications.calls, 1)
        repeated = self.service.publish(
            "KCAMP-TEST", "KCW-VPN",
            expected_draft_fingerprint=complete["draft_fingerprint"],
        )
        self.assertTrue(repeated["published"])
        self.assertEqual(self.publications.calls, 1)

    def test_consolidated_review_get_is_read_only(self):
        self.service.prepare("KCAMP-TEST", "KCW-VPN")
        projection = self.service.project("KCAMP-TEST", "KCW-VPN")
        facade = Mock()
        facade.project.return_value = projection
        flask_app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        with patch("app.app.KnowledgeBuilderService", return_value=facade):
            response = flask_app.test_client().get(
                "/curator/growth/knowledge-builder/KCAMP-TEST/KCW-VPN"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Approve Workflow", response.data)
        self.assertIn(b"Sources, evidence, claims, and technical details", response.data)
        facade.prepare.assert_not_called()
        facade.approve_workflow.assert_not_called()
        facade.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
