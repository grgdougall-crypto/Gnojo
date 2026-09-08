import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.app import app as flask_app
from app.services.knowledge_campaign_orchestration_service import (
    ACTION_POLICY,
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_workflow_generation_service import KnowledgeWorkflowGenerationService


class Planner:
    def __init__(self, campaign): self.campaign = deepcopy(campaign); self.analyzed = 0
    def get(self, campaign_id):
        assert campaign_id == self.campaign["campaign_id"]
        return deepcopy(self.campaign)
    def analyze(self, campaign_id):
        self.analyzed += 1
        self.campaign.update(status="analyzed", last_analyzed_at="now")
        return self.get(campaign_id)


class Store:
    def __init__(self): self.items = []; self.calls = []
    def list_for_campaign(self, campaign_id): return deepcopy(self.items)
    def list_for_research(self, package_id): return deepcopy(self.items)
    def list_for_kdg(self, package_id): return deepcopy(self.items)


class Research(Store):
    def create(self, campaign_id, gap_id, work_item_id):
        self.calls.append(("create", work_item_id))
        value = {"package_id": "KRP-1", "work_item_id": work_item_id, "status": "pending",
                 "selected_sources": []}
        self.items.append(value); return value
    def run(self, package_id):
        self.calls.append(("run", package_id)); self.items[0]["status"] = "ready_for_review"


class Evidence(Store):
    def prepare(self, research_id, source_id):
        existing = next((item for item in self.items
                         if item.get("source_candidate_id") == source_id), None)
        if existing:
            self.calls.append(("prepare", source_id))
            return deepcopy(existing)
        value = {"extraction_id": f"KEX-{len(self.items) + 1}",
                 "source_candidate_id": source_id, "status": "proposed"}
        self.items.append(value); self.calls.append(("prepare", source_id)); return value
    def extract(self, extraction_id):
        self.items[0]["status"] = "needs_review"; self.calls.append(("extract", extraction_id))


class Generation(Store):
    def prepare(self, campaign_id, gap_id, work_item_id):
        value = {"package_id": "KDG-1", "work_item_id": work_item_id}
        self.items.append(value); self.calls.append(("prepare", work_item_id)); return value


class Claims(Store):
    def prepare(self, package_id):
        value = {"claim_plan_id": "KCPM-1", "status": "proposed"}
        self.items.append(value); self.calls.append(("prepare", package_id)); return value
    def plan(self, plan_id): self.items[0]["status"] = "needs_review"; self.calls.append(("plan", plan_id))
    def prepare_workflow(self, campaign_id, work_item_id):
        value = {"claim_plan_id": "KCPM-WORKFLOW-1", "work_item_id": work_item_id,
                 "target_asset_type": "workflow", "status": "proposed"}
        self.items.append(value); self.calls.append(("prepare_workflow", work_item_id)); return value


class Assembly(Store):
    def assemble(self, plan_id):
        value = {"assembly_id": "KASM-1", "status": "ready_for_review"}
        self.items.append(value); self.calls.append(("assemble", plan_id)); return value


class Workflows(Store):
    def eligibility(self, campaign_id, work_item_id):
        return {"eligible": True, "reasons": []}
    def prepare(self, campaign_id, work_item_id):
        value = {"generation_id": "KWG-1", "work_item_id": work_item_id,
                 "status": "prepared", "effective_status": "prepared"}
        self.items.append(value); self.calls.append(("prepare", work_item_id)); return value
    def plan(self, generation_id):
        self.items[0].update(status="plan_ready", effective_status="plan_ready")
        self.calls.append(("plan", generation_id))
    def prepare_draft(self, generation_id):
        self.items[0].update(status="draft_ready", effective_status="draft_ready")
        self.calls.append(("draft", generation_id))


def campaign_fixture():
    return {"campaign_id": "KCAMP-TEST", "title": "Connectivity", "objective": "Build coverage",
            "status": "analyzed", "last_analyzed_at": "now", "reuse_opportunities": [],
            "work_items": [{"work_item_id": "KCW-1", "gap_id": "KCG-1",
                            "work_type": "knowledge_article", "area_id": "dns",
                            "priority": "medium", "status": "proposed"}]}


def factory_fixture(root, campaign=None):
    campaign = campaign or campaign_fixture()
    planner, research, evidence = Planner(campaign), Research(), Evidence()
    generation, claims, assembly, workflows = Generation(), Claims(), Assembly(), Workflows()
    service = KnowledgeCampaignOrchestrationService(
        root, root / "campaigns", planner=planner, research=research,
        evidence=evidence, generation=generation, claims=claims, assembly=assembly,
        workflows=workflows, max_transitions=20, max_external_operations=1)
    return service, planner, research, evidence, generation, claims, assembly, workflows


class KnowledgeCampaignOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.factory = factory_fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_identity_modes_and_policy(self):
        service, *_ = self.factory
        first = service.get_or_create("KCAMP-TEST", "manual")
        second = service.get_or_create("KCAMP-TEST")
        self.assertEqual(first["orchestration_id"], second["orchestration_id"])
        self.assertTrue((service.package_root / f"{first['orchestration_id']}.json").exists())
        with self.assertRaises(KnowledgeCampaignOrchestrationError):
            service.continue_campaign(first["orchestration_id"])
        self.assertEqual(service.set_mode(first["orchestration_id"], "supervised")["mode"], "supervised")
        self.assertEqual(ACTION_POLICY["prepare_research"]["authority"], "machine_safe")
        self.assertEqual(ACTION_POLICY["publish"]["authority"], "human_gate")

    def test_article_path_stops_at_source_gate_and_respects_limit(self):
        service, _, research, *_ = self.factory
        record = service.get_or_create("KCAMP-TEST")
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(research.calls, [("create", "KCW-1")])
        self.assertEqual(result["work_item_states"][0]["next_action"], "run_source_research")

        limited = factory_fixture(self.root / "limited")
        limited[0].limits["max_external_operations"] = 0
        record = limited[0].get_or_create("KCAMP-TEST")
        limited[0].continue_campaign(record["orchestration_id"])
        result = limited[0].continue_campaign(record["orchestration_id"])
        self.assertEqual(result["execution"]["outcomes"][-1]["status"], "limit_reached")

    def test_single_item_advancement(self):
        service, _, research, *_ = self.factory
        record = service.get_or_create("KCAMP-TEST")
        result = service.advance_item(record["orchestration_id"], "KCW-1")
        self.assertEqual(research.calls, [("create", "KCW-1")])
        self.assertEqual(result["work_item_states"][0]["next_action"], "run_source_research")

    def test_autonomous_actor_is_recorded_without_changing_default_authority(self):
        service, *_ = self.factory
        record = service.get_or_create("KCAMP-TEST", actor="Autonomous Growth Stage 1")
        self.assertEqual(service.get(record["orchestration_id"])["history"][0]["actor"],
                         "Autonomous Growth Stage 1")
        service.advance_item(record["orchestration_id"], "KCW-1",
                             actor="Autonomous Growth Stage 1")
        self.assertEqual(service.get(record["orchestration_id"])["history"][-1]["actor"],
                         "Autonomous Growth Stage 1")
        self.assertEqual(ACTION_POLICY["publish"]["authority"], "human_gate")

    def test_persisted_orchestration_reader_is_read_only(self):
        service, *_ = self.factory
        record = service.get_or_create("KCAMP-TEST", "manual")
        path = service.package_root / f"{record['orchestration_id']}.json"
        before = path.read_bytes()

        records = KnowledgeCampaignOrchestrationService.read_persisted(
            service.campaign_root
        )

        self.assertEqual([item["orchestration_id"] for item in records], [
            record["orchestration_id"]
        ])
        self.assertEqual(path.read_bytes(), before)

    def test_existing_orchestration_detail_get_renders_persisted_state(self):
        projection = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Build coverage", "status": "active",
            "mode": "supervised",
            "readiness_summary": {"completion_percent": 0, "machine_ready": 0,
                                  "human_review": 0, "blocked": 0},
            "pipeline_summary": {}, "next_recommended_action": None,
            "work_item_states": [], "human_review_queue": [], "blockers": [],
            "stale_dependencies": [], "dependency_graph": {"edges": []},
            "history": [],
        }
        planner = Mock()
        planner.get.return_value = campaign_fixture()
        flask_app.config.update(TESTING=True)
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch.object(
                KnowledgeCampaignOrchestrationService,
                "read_persisted",
                return_value=[projection],
            ),
            patch("app.app.KnowledgeCoveragePlannerService", return_value=planner),
        ):
            response = flask_app.test_client().get(
                "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Campaign Control Center", response.data)
        self.assertIn(b"KORCH-TEST", response.data)
        self.assertNotIn(b"Control Center not started", response.data)

    def test_missing_orchestration_detail_get_is_repeatably_read_only(self):
        planner = Mock()
        planner.get.return_value = campaign_fixture()
        before = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        flask_app.config.update(TESTING=True)
        return_to = "/review?item=curator_task%3AGKT-TEST"
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch.object(
                KnowledgeCampaignOrchestrationService,
                "read_persisted",
                return_value=[],
            ),
            patch.object(
                KnowledgeCampaignOrchestrationService, "get_or_create"
            ) as create,
            patch("app.app.KnowledgeCoveragePlannerService", return_value=planner),
        ):
            client = flask_app.test_client()
            first = client.get(
                "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration",
                query_string={"return_to": return_to},
            )
            second = client.get(
                "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration",
                query_string={"return_to": return_to},
            )
        after = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn(b"Control Center not started", first.data)
        self.assertIn(b"Return to Coverage Campaign", first.data)
        self.assertIn(b"Return to Review", first.data)
        self.assertIn(return_to.encode(), first.data)
        self.assertEqual(after, before)
        create.assert_not_called()

    def test_learning_gate_links_to_exact_workflow_with_campaign_return_context(self):
        campaign = campaign_fixture()
        campaign["work_items"][0].update({
            "work_type": "learning_content",
            "workflow_id": "low_storage",
            "workflow_filename": "low_storage.json",
            "workflow_lifecycle": "built_in",
            "area_title": "Low Disk Space",
            "coverage_percent": 32,
        })
        built_in_root = self.root / "app" / "decision_trees"
        built_in_root.mkdir(parents=True)
        (built_in_root / "low_storage.json").write_text(json.dumps({
            "workflow_id": "low_storage", "name": "Low Disk Space",
            "start_node": "start",
            "nodes": {"start": {"type": "resolution", "message": "Done"}},
        }), encoding="utf-8")
        projection = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Improve Low Disk Space learning guidance.",
            "status": "awaiting_human_review", "mode": "supervised",
            "readiness_summary": {"completion_percent": 0, "machine_ready": 0,
                                  "human_review": 1, "blocked": 0},
            "pipeline_summary": {}, "next_recommended_action": None,
            "work_item_states": [{
                "work_item_id": "KCW-1", "work_type": "learning_content",
                "title": "Low Storage", "stage": "learning_authoring_required",
                "state": "awaiting_human_review", "next_action": "author_learning_content",
                "action_authority": "human_gate", "review_link": "/workflow-studio",
                "blocker": None,
            }],
            "human_review_queue": [{
                "work_item_id": "KCW-1", "title": "Low Storage",
                "action": "author_learning_content", "review_link": "/workflow-studio",
            }],
            "blockers": [], "stale_dependencies": [],
            "dependency_graph": {"edges": []}, "history": [],
        }
        planner = Mock()
        planner.get.return_value = campaign
        before = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        flask_app.config.update(TESTING=True)
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch.object(
                KnowledgeCampaignOrchestrationService,
                "read_persisted",
                return_value=[deepcopy(projection)],
            ),
            patch("app.app.KnowledgeCoveragePlannerService", return_value=planner),
        ):
            response = flask_app.test_client().get(
                "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration"
            )
        after = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        rendered = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("/workflow-studio?workflow=low_storage", rendered)
        self.assertIn("campaign_id=KCAMP-TEST", rendered)
        self.assertIn("work_item_id=KCW-1", rendered)
        self.assertIn("#workflow-low_storage", rendered)
        self.assertNotIn("This blocker has no governed internal destination.", rendered)
        self.assertEqual(after, before)

    def test_workflow_studio_renders_exact_learning_target_and_return_path_read_only(self):
        campaign = campaign_fixture()
        campaign["work_items"][0].update({
            "work_type": "learning_content", "workflow_id": "low_storage",
            "area_title": "Low Disk Space", "coverage_percent": 32,
        })
        planner = Mock()
        planner.get.return_value = campaign
        drafts = Mock()
        drafts.list_drafts.return_value = []
        return_to = "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration"
        before = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch("app.app.KnowledgeCoveragePlannerService", return_value=planner),
            patch("app.app.WorkflowDraftService", return_value=drafts),
        ):
            response = flask_app.test_client().get(
                "/workflow-studio",
                query_string={
                    "workflow": "low_storage", "campaign_id": "KCAMP-TEST",
                    "work_item_id": "KCW-1", "return_to": return_to,
                },
            )
        after = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        rendered = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Campaign learning review: Low Disk Space", rendered)
        self.assertIn("Learning coverage is 32%", rendered)
        self.assertIn("Campaign target", rendered)
        self.assertIn(f'href="{return_to}"', rendered)
        self.assertEqual(after, before)

    def test_workflow_studio_learning_context_fails_closed_on_identity_mismatch(self):
        campaign = campaign_fixture()
        planner = Mock()
        planner.get.return_value = campaign
        with (
            patch("app.app._structural_repository_root", return_value=self.root),
            patch("app.app.KnowledgeCoveragePlannerService", return_value=planner),
        ):
            response = flask_app.test_client().get(
                "/workflow-studio",
                query_string={
                    "workflow": "low_storage", "campaign_id": "KCAMP-TEST",
                    "work_item_id": "KCW-1",
                    "return_to": "/curator/growth/coverage-campaigns/KCAMP-TEST/orchestration",
                },
            )
        self.assertEqual(response.status_code, 404)

    def test_stage2_learning_and_command_plans_stop_at_specialized_human_gates(self):
        learning = campaign_fixture()
        learning["work_items"][0].update({
            "work_type": "learning_content", "workflow_id": "network",
            "workflow_filename": "network.json", "workflow_lifecycle": "draft",
        })
        learning_root = self.root / "learning"
        draft_root = learning_root / "app" / "workflow_drafts"
        draft_root.mkdir(parents=True)
        (draft_root / "network.json").write_text(json.dumps({
            "workflow_id": "network", "name": "Network", "start_node": "start",
            "nodes": {"start": {"type": "resolution", "message": "Done"}},
        }), encoding="utf-8")
        service, *_ = factory_fixture(learning_root, learning)
        record = service.get_or_create("KCAMP-TEST")
        state = record["work_item_states"][0]
        self.assertEqual(state["next_action"], "author_learning_content")
        self.assertEqual(state["review_link"], "/workflow-studio?workflow=network")
        self.assertEqual(state["review_destination"], {
            "resolved": True,
            "owner": "workflow",
            "resource_type": "learning_authoring_workflow",
            "resource_id": "network",
            "endpoint": "workflow_studio",
            "route_values": {"workflow": "network", "_anchor": "workflow-network"},
        })
        self.assertEqual(state["action_authority"], "human_gate")

        command = campaign_fixture()
        command["work_items"][0].update({
            "work_type": "command_reference", "command_identity": "ipconfig",
        })
        service, *_ = factory_fixture(self.root / "command", command)
        state = service.get_or_create("KCAMP-TEST")["work_item_states"][0]
        self.assertEqual(state["next_action"], "review_command_reference")
        self.assertEqual(state["review_link"], "/commands/ipconfig")
        self.assertEqual(ACTION_POLICY["review_command_reference"]["authority"], "human_gate")

    def test_unresolved_learning_workflow_fails_closed_without_generic_destination(self):
        campaign = campaign_fixture()
        campaign["work_items"][0].update({
            "work_type": "learning_content",
            "workflow_id": "missing_workflow",
            "workflow_filename": "missing_workflow.json",
            "workflow_lifecycle": "built_in",
        })
        service, *_ = factory_fixture(self.root / "missing-learning", campaign)
        state = service.get_or_create("KCAMP-TEST")["work_item_states"][0]
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["blocker"]["blocker_type"], "learning_authoring_target")
        self.assertIsNone(state["review_link"])

    def test_evidence_and_claim_human_gates(self):
        service, _, research, evidence, generation, claims, *_ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1", "status": "approved",
                           "selected_sources": ["SRC-1"]}]
        record = service.get_or_create("KCAMP-TEST")
        service.continue_campaign(record["orchestration_id"])
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(result["work_item_states"][0]["next_action"], "review_evidence")

        evidence.items[0]["status"] = "approved"
        generation.items = [{"package_id": "KDG-1", "work_item_id": "KCW-1"}]
        service.continue_campaign(record["orchestration_id"])
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(claims.calls, [("prepare", "KDG-1"), ("plan", "KCPM-1")])
        self.assertEqual(result["work_item_states"][0]["next_action"], "review_claims")

    def test_empty_confirmed_candidate_set_blocks_claim_planning_as_insufficient_evidence(self):
        service, _, research, evidence, generation, claims, *_ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1",
                           "status": "approved", "selected_sources": ["SRC-1"]}]
        evidence.items = [{"extraction_id": "KEX-EMPTY", "source_candidate_id": "SRC-1",
                           "status": "insufficient_evidence"}]
        record = service.get_or_create("KCAMP-TEST")

        result = service.refresh(record["orchestration_id"])
        state = result["work_item_states"][0]
        self.assertEqual(state["stage"], "insufficient_evidence")
        self.assertEqual(state["state"], "blocked")
        self.assertIsNone(state["next_action"])
        self.assertEqual(state["review_link"],
                         "/curator/growth/evidence-extraction/KEX-EMPTY")
        self.assertIn("no Candidate Evidence", state["blocker"]["explanation"])
        self.assertEqual(generation.items, [])
        self.assertEqual(claims.items, [])

    def test_two_source_evidence_preparation_reports_progress_and_packages(self):
        service, _, research, evidence, *_ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1", "status": "approved",
                           "selected_sources": ["SRC-ETHERNET", "SRC-NETADAPTER"]}]
        record = service.get_or_create("KCAMP-TEST")

        first = service.advance_item(record["orchestration_id"], "KCW-1")
        first_outcome = first["execution"]["outcomes"][0]
        first_item = first["work_item_states"][0]
        self.assertEqual(first_outcome["source_candidate_id"], "SRC-ETHERNET")
        self.assertEqual(first_outcome["extraction_id"], "KEX-1")
        self.assertEqual(first_outcome["package_disposition"], "created")
        self.assertEqual(first_item["evidence_progress"], {
            "prepared_sources": 1, "total_sources": 2, "remaining_sources": 1,
        })
        self.assertEqual(first_item["next_action"], "prepare_evidence")
        self.assertEqual(first_item["evidence_packages"][0]["review_link"],
                         "/curator/growth/evidence-extraction/KEX-1")

        second = service.advance_item(record["orchestration_id"], "KCW-1")
        second_outcome = second["execution"]["outcomes"][0]
        second_item = second["work_item_states"][0]
        self.assertEqual(second_outcome["source_candidate_id"], "SRC-NETADAPTER")
        self.assertEqual(second_outcome["extraction_id"], "KEX-2")
        self.assertEqual(second_item["evidence_progress"]["prepared_sources"], 2)
        self.assertEqual(second_item["next_action"], "extract_evidence")
        self.assertEqual([item["extraction_id"] for item in second_item["evidence_packages"]],
                         ["KEX-1", "KEX-2"])
        self.assertEqual(evidence.calls, [("prepare", "SRC-ETHERNET"),
                                          ("prepare", "SRC-NETADAPTER")])

    def test_item_advance_failure_is_recorded_as_failure_not_success(self):
        service, _, research, *_ = self.factory
        research.create = Mock(side_effect=RuntimeError("Extraction service unavailable."))
        record = service.get_or_create("KCAMP-TEST")
        result = service.advance_item(record["orchestration_id"], "KCW-1")
        self.assertEqual(result["execution"]["outcomes"][0]["status"], "failed")
        persisted = service.get(record["orchestration_id"])
        self.assertEqual(persisted["history"][-1]["event"], "work_item_advance_failed")

    def test_idempotent_prepare_is_explicitly_recorded_as_package_reused(self):
        service, _, research, *_ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1", "status": "approved",
                           "selected_sources": ["SRC-ETHERNET"]}]
        record = service.get_or_create("KCAMP-TEST")
        reused = {"work_item_id": "KCW-1", "action": "prepare_evidence",
                  "status": "package_reused", "source_candidate_id": "SRC-ETHERNET",
                  "extraction_id": "KEX-1", "package_disposition": "reused"}
        with patch.object(service, "_execute", return_value=reused):
            result = service.advance_item(record["orchestration_id"], "KCW-1")
        self.assertEqual(result["execution"]["outcomes"][0]["status"], "package_reused")
        persisted = service.get(record["orchestration_id"])
        self.assertEqual(persisted["history"][-1]["event"], "package_reused")

    def test_article_assembly_stops_at_review(self):
        service, _, research, evidence, generation, claims, assembly, _ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1", "status": "approved",
                           "selected_sources": ["SRC-1"]}]
        evidence.items = [{"extraction_id": "KEX-1", "source_candidate_id": "SRC-1", "status": "approved"}]
        generation.items = [{"package_id": "KDG-1", "work_item_id": "KCW-1"}]
        claims.items = [{"claim_plan_id": "KCPM-1", "status": "ready_for_drafting"}]
        record = service.get_or_create("KCAMP-TEST")
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(assembly.calls, [("assemble", "KCPM-1")])
        self.assertEqual(result["work_item_states"][0]["next_action"], "review_article_draft")

    def test_workflow_routing_stops_at_review(self):
        campaign = campaign_fixture()
        campaign["work_items"][0]["work_type"] = "workflow"
        workflows = Workflows()
        service = KnowledgeCampaignOrchestrationService(
            self.root, self.root / "workflow-campaigns", planner=Planner(campaign), research=Research(),
            evidence=Evidence(), generation=Generation(), claims=Claims(), assembly=Assembly(), workflows=workflows)
        record = service.get_or_create("KCAMP-TEST")
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(workflows.calls, [("prepare", "KCW-1")])
        self.assertEqual(result["work_item_states"][0]["next_action"], "plan_workflow")

    def test_workflow_item_starts_supervised_evidence_chain_when_phase_eight_is_not_eligible(self):
        campaign = campaign_fixture()
        campaign["work_items"][0]["work_type"] = "workflow"
        workflows = Workflows()
        workflows.eligibility = lambda campaign_id, work_item_id: {
            "eligible": False, "reasons": ["Approved current workflow claims are required."]
        }
        service = KnowledgeCampaignOrchestrationService(
            self.root, self.root / "ineligible-workflow-campaigns", planner=Planner(campaign),
            research=Research(), evidence=Evidence(), generation=Generation(), claims=Claims(),
            assembly=Assembly(), workflows=workflows)
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual(record["readiness_summary"]["machine_ready"], 1)
        self.assertEqual(record["readiness_summary"]["blocked"], 0)
        self.assertEqual(record["work_item_states"][0]["next_action"], "prepare_research")

    def test_continue_advances_only_the_displayed_machine_ready_workflow_item(self):
        campaign = campaign_fixture()
        campaign["work_items"] = [
            {**campaign["work_items"][0], "work_item_id": "KCW-DNS", "gap_id": "KCG-DNS",
             "work_type": "workflow", "area_id": "dns"},
            {**campaign["work_items"][0], "work_item_id": "KCW-SECOND", "gap_id": "KCG-SECOND",
             "work_type": "workflow", "area_id": "proxy"},
        ]
        workflows = Workflows()
        service = KnowledgeCampaignOrchestrationService(
            self.root, self.root / "one-workflow-campaigns", planner=Planner(campaign),
            research=Research(), evidence=Evidence(), generation=Generation(), claims=Claims(),
            assembly=Assembly(), workflows=workflows)
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual(record["next_recommended_action"]["work_item_id"], "KCW-DNS")
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(workflows.calls, [("prepare", "KCW-DNS")])
        self.assertEqual(result["execution"]["transitions"], 1)
        dns = next(item for item in result["work_item_states"] if item["work_item_id"] == "KCW-DNS")
        second = next(item for item in result["work_item_states"] if item["work_item_id"] == "KCW-SECOND")
        self.assertEqual(dns["package_id"], "KWG-1")
        self.assertEqual(dns["next_action"], "plan_workflow")
        self.assertEqual(second["next_action"], "prepare_workflow_package")
        self.assertEqual(result["history"][-1]["event"], "campaign_continued")
        self.assertEqual(result["history"][-1]["outcomes"][0]["status"], "completed")

    def test_supervised_continue_integrates_with_phase_eight_and_persists_kwg_package(self):
        campaign = campaign_fixture()
        campaign["work_items"][0].update(
            work_type="workflow", area_id="dns", target_asset="dns-diagnostics"
        )
        campaign_root = self.root / "integration-campaigns"
        campaign_root.mkdir(parents=True)
        (campaign_root / "claim_planning").mkdir()
        (campaign_root / "research").mkdir()
        (campaign_root / "evidence_extraction").mkdir()
        (self.root / "app" / "decision_trees").mkdir(parents=True)
        (self.root / "app" / "workflow_drafts").mkdir(parents=True)
        (campaign_root / "KCAMP-TEST.json").write_text(json.dumps(campaign), encoding="utf-8")
        claim = {
            "claim_id": "CLM-DNS", "review_state": "approved", "stale": False,
            "evidence_ids": ["EVD-DNS"], "source_urls": ["https://learn.microsoft.com/windows"],
            "workflow_spec": {
                "node_id": "dns_result", "type": "resolution", "start_node": "dns_result",
                "workflow_name": "DNS Diagnostics", "category": "Networking", "platform": "Windows",
                "fields": {"title": "DNS Diagnostics Complete", "message": "DNS evidence was recorded."},
            },
        }
        (campaign_root / "claim_planning" / "KCPM-DNS.json").write_text(json.dumps({
            "claim_plan_id": "KCPM-DNS", "campaign_id": "KCAMP-TEST", "work_item_id": "KCW-1",
            "target_asset_type": "workflow", "status": "ready_for_drafting",
            "approved_evidence_ids": ["EVD-DNS"], "claims": [claim],
        }), encoding="utf-8")
        (campaign_root / "research" / "KSR-DNS.json").write_text(json.dumps({
            "package_id": "KSR-DNS", "campaign_id": "KCAMP-TEST", "work_item_id": "KCW-1",
            "status": "approved",
        }), encoding="utf-8")
        (campaign_root / "evidence_extraction" / "KEX-DNS.json").write_text(json.dumps({
            "extraction_id": "KEX-DNS", "research_package_id": "KSR-DNS", "status": "approved",
            "evidence_units": [{"evidence_id": "EVD-DNS", "review_state": "approved"}],
        }), encoding="utf-8")
        workflows = KnowledgeWorkflowGenerationService(
            self.root, campaign_root, self.root / "app" / "workflow_drafts"
        )
        service = KnowledgeCampaignOrchestrationService(
            self.root, campaign_root, planner=Planner(campaign), research=Research(), evidence=Evidence(),
            generation=Generation(), claims=Claims(), assembly=Assembly(), workflows=workflows)
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual(record["next_recommended_action"]["next_action"], "prepare_workflow_package")
        result = service.continue_campaign(record["orchestration_id"])
        packages = workflows.list_for_campaign("KCAMP-TEST")
        self.assertEqual(len(packages), 1)
        self.assertRegex(packages[0]["generation_id"], r"^KWG-[A-F0-9]{12}$")
        self.assertEqual(packages[0]["work_item_id"], "KCW-1")
        self.assertEqual(result["work_item_states"][0]["package_id"], packages[0]["generation_id"])
        self.assertEqual(result["work_item_states"][0]["next_action"], "plan_workflow")
        persisted = json.loads((service.package_root / f"{record['orchestration_id']}.json").read_text())
        self.assertEqual(persisted["history"][-1]["outcomes"][0]["status"], "completed")

    def test_continue_route_posts_to_service_and_surfaces_success(self):
        projection = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Build coverage", "status": "active", "mode": "supervised",
            "readiness_summary": {"completion_percent": 25, "machine_ready": 1,
                                  "human_review": 0, "blocked": 0},
            "pipeline_summary": {}, "next_recommended_action": None, "work_item_states": [],
            "human_review_queue": [], "blockers": [], "stale_dependencies": [],
            "dependency_graph": {"edges": []}, "history": [],
            "execution": {"outcomes": [{"work_item_id": "KCW-DNS", "action": "prepare_workflow_package",
                                          "status": "completed"}], "transitions": 1},
        }
        service = Mock()
        service.continue_campaign.return_value = deepcopy(projection)
        service.get_or_create.return_value = deepcopy(projection)
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.KnowledgeCampaignOrchestrationService", return_value=service,
        ) as orchestration_type:
            orchestration_type.read_persisted.return_value = [deepcopy(projection)]
            response = flask_app.test_client().post(
                "/curator/growth/orchestration/KORCH-TEST/continue",
                data={"campaign_id": "KCAMP-TEST"}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        service.continue_campaign.assert_called_once_with("KORCH-TEST")
        self.assertIn(b"Campaign advanced one recommended work item.", response.data)

    def test_continue_route_surfaces_phase_failure(self):
        projection = {
            "campaign_id": "KCAMP-TEST",
            "execution": {"outcomes": [{"work_item_id": "KCW-DNS", "action": "prepare_workflow_package",
                                          "status": "failed", "message": "Eligibility changed."}]},
        }
        service = Mock()
        service.continue_campaign.return_value = projection
        detail = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Build coverage", "status": "blocked", "mode": "supervised",
            "readiness_summary": {"completion_percent": 25, "machine_ready": 0,
                                  "human_review": 0, "blocked": 1},
            "pipeline_summary": {}, "next_recommended_action": None, "work_item_states": [],
            "human_review_queue": [], "blockers": [], "stale_dependencies": [],
            "dependency_graph": {"edges": []}, "history": [],
        }
        service.get_or_create.return_value = detail
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.KnowledgeCampaignOrchestrationService", return_value=service,
        ) as orchestration_type:
            orchestration_type.read_persisted.return_value = [deepcopy(detail)]
            response = flask_app.test_client().post(
                "/curator/growth/orchestration/KORCH-TEST/continue",
                data={"campaign_id": "KCAMP-TEST"}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Eligibility changed.", response.data)

    def test_item_advance_route_surfaces_created_evidence_package_and_progress(self):
        projection = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Build coverage", "status": "active", "mode": "supervised",
            "readiness_summary": {"completion_percent": 25, "machine_ready": 1,
                                  "human_review": 0, "blocked": 0},
            "pipeline_summary": {"evidence_extraction_ready": 1},
            "next_recommended_action": None,
            "work_item_states": [{
                "work_item_id": "KCW-1", "work_type": "knowledge_article", "title": "Ethernet",
                "stage": "evidence_extraction_ready", "state": "machine_ready",
                "next_action": "prepare_evidence", "action_authority": "machine_safe",
                "blocker": None, "blocker_destination": None,
                "evidence_progress": {"prepared_sources": 1, "total_sources": 2,
                                      "remaining_sources": 1},
                "evidence_packages": [{"source_candidate_id": "SRC-ETHERNET",
                                       "extraction_id": "KEX-1", "status": "proposed",
                                       "review_link": "/curator/growth/evidence-extraction/KEX-1"}],
            }],
            "human_review_queue": [], "blockers": [], "stale_dependencies": [],
            "dependency_graph": {"edges": []}, "history": [],
            "execution": {"outcomes": [{"work_item_id": "KCW-1", "action": "prepare_evidence",
                                           "status": "completed", "source_candidate_id": "SRC-ETHERNET",
                                           "extraction_id": "KEX-1", "package_disposition": "created"}]},
        }
        service = Mock()
        service.advance_item.return_value = deepcopy(projection)
        service.get_or_create.return_value = deepcopy(projection)
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.KnowledgeCampaignOrchestrationService", return_value=service,
        ) as orchestration_type:
            orchestration_type.read_persisted.return_value = [deepcopy(projection)]
            response = flask_app.test_client().post(
                "/curator/growth/orchestration/KORCH-TEST/items/KCW-1/advance",
                data={"campaign_id": "KCAMP-TEST"}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Evidence package KEX-1 created for source SRC-ETHERNET.", response.data)
        self.assertIn(b"1 of 2 sources prepared", response.data)
        self.assertIn(b"KEX-1", response.data)

    def test_item_advance_route_surfaces_execution_failure(self):
        outcome = {"work_item_id": "KCW-1", "action": "prepare_evidence", "status": "failed",
                   "message": "Source retrieval failed."}
        service = Mock()
        service.advance_item.return_value = {
            "campaign_id": "KCAMP-TEST", "execution": {"outcomes": [outcome]},
        }
        detail = {
            "orchestration_id": "KORCH-TEST", "campaign_id": "KCAMP-TEST",
            "campaign_objective": "Build coverage", "status": "active", "mode": "supervised",
            "readiness_summary": {"completion_percent": 25, "machine_ready": 1,
                                  "human_review": 0, "blocked": 0},
            "pipeline_summary": {}, "next_recommended_action": None, "work_item_states": [],
            "human_review_queue": [], "blockers": [], "stale_dependencies": [],
            "dependency_graph": {"edges": []}, "history": [],
        }
        service.get_or_create.return_value = detail
        flask_app.config.update(TESTING=True)
        with patch(
            "app.app.KnowledgeCampaignOrchestrationService", return_value=service,
        ) as orchestration_type:
            orchestration_type.read_persisted.return_value = [deepcopy(detail)]
            response = flask_app.test_client().post(
                "/curator/growth/orchestration/KORCH-TEST/items/KCW-1/advance",
                data={"campaign_id": "KCAMP-TEST"}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Source retrieval failed.", response.data)

    def test_reuse_completion_and_content_studio_boundary(self):
        campaign = campaign_fixture()
        campaign["reuse_opportunities"] = [{"opportunity_id": "KCR-1", "article_id": "dns-basics",
                                             "workflow_ids": ["a", "b"], "areas": ["dns"],
                                             "evidence": ["shared"]}]
        root = self.root / "reuse"
        published = root / "knowledge_base" / "published"
        published.mkdir(parents=True)
        (published / "dns-basics.json").write_text(json.dumps({
            "id": "dns-basics", "canonical_id": "dns-basics", "title": "DNS Basics",
            "review_status": "approved"
        }), encoding="utf-8")
        service, *_ = factory_fixture(root, campaign)
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual((record["status"], record["work_item_states"][0]["stage"]),
                         ("completed", "reuse_available"))
        self.assertNotIn("publish", [event["event"] for event in record["history"]])

    def test_blockers_stale_state_and_dependency_graph(self):
        service, _, research, *_ = self.factory
        research.items = [{"package_id": "KRP-1", "work_item_id": "KCW-1", "status": "needs_refresh",
                           "selected_sources": []}]
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual(len(record["blockers"]), 1)
        self.assertEqual(len(record["stale_dependencies"]), 1)
        self.assertIn({"from": "KCW-1", "to": "KRP-1"}, record["dependency_graph"]["edges"])

    def test_refresh_is_idempotent(self):
        service, *_ = self.factory
        record = service.get_or_create("KCAMP-TEST")
        before = len(record["history"])
        service.refresh(record["orchestration_id"])
        self.assertEqual(len(service.refresh(record["orchestration_id"])["history"]), before)

    def test_fresh_empty_campaign_analysis(self):
        campaign = campaign_fixture()
        campaign.update(status="draft", last_analyzed_at=None, work_items=[])
        planner = Planner(campaign)
        service, *_ = factory_fixture(self.root / "fresh", campaign)
        service.planner = planner
        record = service.get_or_create("KCAMP-TEST")
        self.assertEqual(record["next_recommended_action"]["next_action"], "analyze_coverage")
        service.continue_campaign(record["orchestration_id"])
        self.assertEqual(planner.analyzed, 1)

    def test_failure_isolation_and_no_autonomy(self):
        service, _, research, *_ = self.factory
        def fail(*args): raise RuntimeError("safe failure")
        research.create = fail
        record = service.get_or_create("KCAMP-TEST")
        result = service.continue_campaign(record["orchestration_id"])
        self.assertEqual(len(result["execution"]["outcomes"]), 1)
        self.assertEqual(result["execution"]["outcomes"][0]["status"], "failed")
        for name in ("start_background", "schedule", "approve", "publish", "auto_publish"):
            self.assertFalse(hasattr(service, name))


if __name__ == "__main__": unittest.main()
