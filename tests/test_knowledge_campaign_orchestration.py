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
        value = {"extraction_id": "KEX-1", "source_candidate_id": source_id, "status": "proposed"}
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

    def test_workflow_item_is_not_machine_ready_when_phase_eight_rejects_eligibility(self):
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
        self.assertEqual(record["readiness_summary"]["machine_ready"], 0)
        self.assertEqual(record["readiness_summary"]["blocked"], 1)
        self.assertEqual(record["work_item_states"][0]["blocker"]["blocker_type"], "workflow_eligibility")
        self.assertIn("Approved current workflow claims", record["blockers"][0]["explanation"])

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
            "status": "ready_for_drafting", "claims": [claim],
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
        with patch("app.app.KnowledgeCampaignOrchestrationService", return_value=service):
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
        with patch("app.app.KnowledgeCampaignOrchestrationService", return_value=service):
            response = flask_app.test_client().post(
                "/curator/growth/orchestration/KORCH-TEST/continue",
                data={"campaign_id": "KCAMP-TEST"}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Eligibility changed.", response.data)

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
