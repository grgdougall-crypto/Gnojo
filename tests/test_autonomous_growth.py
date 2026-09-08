import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.services.autonomous_growth_service import (
    AUTONOMOUS_ACTOR,
    AutonomousGrowthService,
)
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.review_workspace_service import ReviewWorkspaceService
from curator.__main__ import main


def assessment(*, article_missing=True, workflow_count=1, coverage=86):
    facets = {"workflow": True, "article": not article_missing}
    gaps = []
    if article_missing:
        gaps.append({
            "gap_id": "KCG-ASSESSMENT",
            "gap_type": "missing_article",
            "area_id": "dns",
            "area_title": "DNS",
            "summary": "DNS has missing article.",
            "confidence": "high",
            "evidence": ["Coverage facet 'article' is not present in the current inventory."],
        })
    return {
        "domain": {"id": "windows-connectivity", "title": "Windows Connectivity"},
        "areas": [{
            "area_id": "dns", "title": "DNS", "facets": facets,
            "workflow_count": workflow_count, "relevant_node_count": 3,
            "coverage_percent": coverage,
        }],
        "gaps": gaps,
        "fingerprint": "assessment-fingerprint",
    }


class Planner:
    def __init__(self, *, campaigns=None, projected=None, extended=None):
        self.campaigns = deepcopy(campaigns or [])
        self.projected = deepcopy(projected or assessment())
        self.extended = deepcopy(extended or [])
        self.created = 0
        self.analyzed = 0

    def domains(self):
        return [{"id": "windows-connectivity"}]

    def assess_domain(self, domain_id):
        return deepcopy(self.projected)

    def list_campaigns(self):
        return deepcopy(self.campaigns)

    def assess_stage2_candidates(self):
        return deepcopy(self.extended)

    def create(self, **values):
        self.created += 1
        campaign = {
            "campaign_id": "KCP-AUTONOMOUS01", "domain": values["domain_id"],
            "status": "draft", "last_analyzed_at": None, "gaps": [], "work_items": [],
            "creation_metadata": deepcopy(values["metadata"]),
            "history": [{"event": "created", "actor": values["actor"]}],
        }
        self.campaigns.append(campaign)
        return deepcopy(campaign)

    def analyze(self, campaign_id):
        self.analyzed += 1
        campaign = next(item for item in self.campaigns if item["campaign_id"] == campaign_id)
        campaign.update(status="analyzed", last_analyzed_at="now")
        selected = (campaign.get("creation_metadata") or {}).get("selected_gap") or {}
        gap_type = selected.get("gap_type") or "missing_article"
        area_id = selected.get("area_id") or "dns"
        campaign["gaps"] = [{
            "gap_id": "KCG-CAMPAIGN", "gap_type": gap_type, "area_id": area_id,
            "gap_identity": selected.get("gap_identity"),
        }]
        work_type = {
            "missing_article": "knowledge_article",
            "weak_learning_coverage": "learning_content",
            "missing_command_reference": "command_reference",
            "missing_workflow": "workflow",
        }[gap_type]
        campaign["work_items"] = [{
            "work_item_id": "KCW-DNS", "gap_id": "KCG-CAMPAIGN",
            "work_type": work_type,
        }]
        return deepcopy(campaign)


class LegacyCommandPlanner(KnowledgeCoveragePlannerService):
    def __init__(self, repository_root, campaign_root, candidate):
        super().__init__(repository_root, campaign_root)
        self.candidate = deepcopy(candidate)

    def domains(self):
        return [{"id": "windows-connectivity"}]

    def assess_domain(self, domain_id):
        return {
            "domain": {"id": domain_id, "title": "Windows Connectivity"},
            "areas": [], "gaps": [], "fingerprint": "domain-clean",
        }

    def assess_stage2_candidates(self):
        return [deepcopy(self.candidate)]


class Orchestration:
    def __init__(self, *, fail=False, initial="machine_safe"):
        self.fail = fail
        self.initial = initial
        self.calls = []
        self.stage = 0

    def get_or_create(self, campaign_id, mode="supervised", actor="Human"):
        self.calls.append(("get_or_create", campaign_id, mode, actor))
        return self._record(campaign_id)

    def advance_item(self, orchestration_id, work_item_id, actor="Human"):
        self.calls.append(("advance_item", work_item_id, actor))
        if self.fail:
            return {**self._record("KCP-AUTONOMOUS01"), "execution": {"outcomes": [{
                "status": "failed", "message": "Pipeline validation failed."
            }]}}
        self.stage += 1
        action = "prepare_research" if self.stage == 1 else "run_source_research"
        return {**self._record("KCP-AUTONOMOUS01"), "execution": {"outcomes": [{
            "status": "completed", "action": action, "package_id": "KRP-ONE"
        }]}}

    def _record(self, campaign_id):
        authority = self.initial if self.stage == 0 else (
            "machine_safe" if self.stage == 1 else "human_gate"
        )
        action = "prepare_research" if self.stage == 0 else (
            "run_source_research" if self.stage == 1 else "approve_source"
        )
        return {
            "orchestration_id": "KORCH-ONE", "campaign_id": campaign_id,
            "work_item_states": [{
                "work_item_id": "KCW-DNS", "state": "ready",
                "action_authority": authority, "next_action": action,
                "stage": "source_approval_required" if authority == "human_gate" else "research_needed",
                "review_link": "/curator/growth/source-research/KRP-ONE" if authority == "human_gate" else None,
                "blocker": None,
            }],
        }


class LegacyGateOrchestration:
    def get_or_create(self, campaign_id, mode="supervised", actor="Human"):
        return {
            "orchestration_id": "KORCH-LEGACY001",
            "campaign_id": campaign_id,
            "work_item_states": [{
                "work_item_id": "KCW-LEGACY001",
                "state": "awaiting_human_review",
                "action_authority": "human_gate",
                "next_action": "review_command_reference",
                "stage": "command_reference_review_required",
                "review_link": "/commands/ipconfig",
            }],
        }


def analyzed_campaign(status="analyzed"):
    return {
        "campaign_id": "KCP-EXISTING01", "domain": "windows-connectivity",
        "status": status, "last_analyzed_at": "now",
        "creation_metadata": {"gap_identity": "windows-connectivity:dns:missing_article"},
        "gaps": [{"gap_id": "KCG-OLD", "gap_type": "missing_article", "area_id": "dns"}],
        "work_items": [{"work_item_id": "KCW-DNS", "gap_id": "KCG-OLD",
                        "work_type": "knowledge_article"}],
    }


def extended_candidate(gap_type="weak_learning_coverage"):
    base = {
        "gap_identity": "workflow:network:weak_learning_coverage",
        "gap_type": gap_type,
        "title": "Improve learning guidance for Network",
        "domain_id": "windows-connectivity",
        "area_id": "network",
        "area_title": "Network",
        "workflow_id": "network",
        "workflow_filename": "network.json",
        "workflow_lifecycle": "draft",
        "confidence": "high",
        "coverage_percent": 25,
        "measurable_deficiency": 75,
        "evidence_strength": 2,
        "runtime_relevance": 0,
        "evidence": ["Learning coverage is 25%.", "Node inspect lacks help text."],
        "assessment_fingerprint": "workflow-fingerprint",
        "node_ids": ["inspect"],
        "intended_artifact": "learning_content_plan",
        "expected_human_gate": "Workflow Designer learning authoring",
    }
    if gap_type == "missing_command_reference":
        base.update({
            "gap_identity": "workflow:network:node:inspect:missing_command_reference:ipconfig",
            "title": "Review ipconfig reference support",
            "node_id": "inspect", "node_ids": [], "article_id": "network-guide",
            "command_identity": "ipconfig", "command_risk": {"level": "Low"},
            "measurable_deficiency": 1, "evidence_strength": 3,
            "intended_artifact": "command_relationship_review",
            "expected_human_gate": "Command Library relationship review",
        })
    return base


class AutonomousGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def service(self, planner=None, orchestration=None, **values):
        planner = planner or Planner()
        return AutonomousGrowthService(
            self.root, self.root / "campaigns", planner=planner,
            orchestration=orchestration or Orchestration(), **values,
        )

    def legacy_command_fixture(self, *, state_overrides=None, duplicate_work=False,
                               reciprocal=False, identities_complete=False,
                               article_declaration_absent=False):
        campaign_root = self.root / "knowledge_campaigns"
        workflow_id, node_id = "network", "inspect"
        article_id, command_id = "network-guide", "ipconfig"
        candidate = extended_candidate("missing_command_reference")

        workflow_path = self.root / "app/decision_trees/network.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(json.dumps({
            "workflow_id": workflow_id,
            "name": "Network Diagnostics",
            "category": "Networking",
            "platform": "Windows",
            "start_node": node_id,
            "nodes": {node_id: {
                "type": "instruction",
                "title": "Inspect network configuration",
                "instruction": "Run ipconfig and inspect the result.",
                "knowledge_article": article_id,
            }},
        }), encoding="utf-8")
        command_path = self.root / "knowledge_base/commands/ipconfig.json"
        command_path.parent.mkdir(parents=True, exist_ok=True)
        command_path.write_text(json.dumps({
            "id": command_id,
            "name": command_id,
            "related_articles": [article_id] if reciprocal else [],
            "risk": {"level": "Low", "changes_system": False},
        }), encoding="utf-8")
        article_path = self.root / "knowledge_base/published/network-guide.json"
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article = {
            "id": article_id,
            "canonical_id": article_id,
            "title": "Network Guide",
            "commands": [{"command": "ipconfig"}],
        }
        if not article_declaration_absent:
            article["related_commands"] = [command_id] if reciprocal else []
        article_path.write_text(json.dumps(article), encoding="utf-8")

        campaign_id, gap_id, work_id = "KCP-LEGACY001", "KCG-LEGACY001", "KCW-LEGACY001"
        selected = deepcopy(candidate)
        campaign = {
            "schema_version": "1.0",
            "campaign_id": campaign_id,
            "title": "Legacy command review",
            "domain": "windows-connectivity",
            "status": "analyzed",
            "last_analyzed_at": "2026-09-01T00:00:00+00:00",
            "creation_metadata": {
                "initiated_by": "autonomous_growth_stage2",
                "gap_identity": candidate["gap_identity"],
                "assessment_fingerprint": candidate["assessment_fingerprint"],
                "selected_gap": selected,
            },
            "gaps": [{
                "gap_id": gap_id,
                "gap_type": "missing_command_reference",
                "area_id": workflow_id,
                "priority": "medium",
                "confidence": "high",
                "evidence": list(candidate["evidence"]),
            }],
            "work_items": [{
                "work_item_id": work_id,
                "campaign_id": campaign_id,
                "gap_id": gap_id,
                "work_type": "command_reference",
                "area_id": workflow_id,
                "priority": "medium",
                "confidence": "high",
                "status": "proposed",
                "evidence": list(candidate["evidence"]),
            }],
            "history": [{"event": "created", "actor": AUTONOMOUS_ACTOR}],
        }
        if identities_complete:
            identity_fields = (
                "gap_identity", "workflow_id", "workflow_filename", "workflow_lifecycle",
                "node_id", "article_id", "command_identity",
            )
            for record in (campaign["gaps"][0], campaign["work_items"][0]):
                record.update({key: candidate[key] for key in identity_fields})
        if duplicate_work:
            campaign["work_items"].append({**campaign["work_items"][0],
                                           "work_item_id": "KCW-LEGACY002"})
        campaign_root.mkdir(parents=True, exist_ok=True)
        campaign_path = campaign_root / f"{campaign_id}.json"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")

        state = {
            "work_item_id": work_id,
            "gap_id": gap_id,
            "title": "Network",
            "work_type": "command_reference",
            "priority": "medium",
            "stage": "command_reference_review_required",
            "state": "awaiting_human_review",
            "next_action": "review_command_reference",
            "action_authority": "human_gate",
            "review_link": "/commands/ipconfig",
        }
        state.update(state_overrides or {})
        orchestration_id = "KORCH-LEGACY001"
        orchestration_path = campaign_root / "orchestration" / f"{orchestration_id}.json"
        orchestration_path.parent.mkdir(parents=True, exist_ok=True)
        orchestration_path.write_text(json.dumps({
            "schema_version": "1.0",
            "orchestration_id": orchestration_id,
            "campaign_id": campaign_id,
            "status": "awaiting_human_review",
            "mode": "supervised",
            "work_item_states": [state],
            "human_review_queue": [],
            "history": [{"event": "orchestration_enabled", "actor": AUTONOMOUS_ACTOR}],
            "fingerprints": {},
        }), encoding="utf-8")
        planner = LegacyCommandPlanner(self.root, campaign_root, candidate)
        service = AutonomousGrowthService(
            self.root,
            campaign_root,
            planner=planner,
            orchestration=LegacyGateOrchestration(),
        )
        return service, planner, candidate, campaign_path, orchestration_path

    def test_deterministic_selection_and_reviewer_readable_explanation(self):
        first = self.service().run(preview=True)
        second = self.service().run(preview=True)
        self.assertEqual(first.selected_gap, second.selected_gap)
        self.assertEqual(first.selected_gap["gap_identity"],
                         "windows-connectivity:dns:missing_article")
        self.assertIn("authoritative workflow", first.selection_explanation)
        self.assertIn("supporting article coverage is missing", first.selection_explanation)

    def test_larger_measured_deficiency_wins_before_stable_tiebreaker(self):
        service = self.service()
        shallow = assessment(coverage=86)
        deeper = assessment(coverage=42)
        deeper["domain"] = {"id": "z-domain", "title": "Z Domain"}
        selected = service._select([shallow, deeper])
        self.assertEqual(selected["domain_id"], "z-domain")

    def test_cross_type_priority_is_deterministic_and_selects_one(self):
        learning = extended_candidate()
        command = extended_candidate("missing_command_reference")
        selected = self.service()._select([], [command, learning])
        self.assertEqual(selected["gap_type"], "weak_learning_coverage")
        self.assertIn("higher-priority supported types", selected["selection_explanation"])

    def test_learning_candidate_requires_threshold_and_stable_node_identity(self):
        candidate = extended_candidate()
        self.assertEqual(self.service()._select([], [candidate])["gap_type"],
                         "weak_learning_coverage")
        candidate["node_ids"] = []
        self.assertIsNone(self.service()._select([], [candidate]))

    def test_command_candidate_requires_structured_identity_and_risk(self):
        candidate = extended_candidate("missing_command_reference")
        self.assertEqual(self.service()._select([], [candidate])["command_identity"], "ipconfig")
        candidate["command_identity"] = ""
        self.assertIsNone(self.service()._select([], [candidate]))

    def test_missing_workflow_requires_converging_article_and_command_support(self):
        projected = assessment(article_missing=False, workflow_count=0, coverage=71)
        projected["areas"][0].update(
            article_count=1, command_count=1, safety_ambiguous_command_count=0,
            asset_ids=["dns-guide", "nslookup"]
        )
        projected["gaps"] = [{
            "gap_id": "KCG-WORKFLOW", "gap_type": "missing_workflow",
            "area_id": "dns", "area_title": "DNS", "summary": "DNS lacks a workflow.",
            "confidence": "high", "evidence": ["Workflow coverage is absent."],
        }]
        selected = self.service()._select([projected])
        self.assertEqual(selected["gap_identity"],
                         "domain:windows-connectivity:topic:dns:missing_workflow")
        projected["areas"][0]["command_count"] = 0
        self.assertIsNone(self.service()._select([projected]))
        projected["areas"][0].update(command_count=1, workflow_count=1)
        self.assertIsNone(self.service()._select([projected]))
        projected["areas"][0].update(workflow_count=0, safety_ambiguous_command_count=1)
        self.assertIsNone(self.service()._select([projected]))

    def test_learning_plan_uses_campaign_and_stops_at_specialized_human_gate(self):
        candidate = extended_candidate()
        planner = Planner(projected=assessment(article_missing=False), extended=[candidate])
        orchestration = Orchestration(initial="human_gate")
        result = self.service(planner, orchestration).run()
        self.assertEqual(result.selected_gap["gap_type"], "weak_learning_coverage")
        self.assertEqual(result.preparation["outcome"], "prepared_for_human_review")
        self.assertEqual(len([call for call in orchestration.calls if call[0] == "advance_item"]), 0)

    def test_command_gate_handoff_targets_the_exact_review_item(self):
        result = AutonomousGrowthService._human_review(
            {"campaign_id": "KCP-COMMAND"},
            {
                "work_item_id": "KCW-COMMAND",
                "action_authority": "human_gate",
                "next_action": "review_command_reference",
                "review_link": "/commands/ipconfig",
            },
        )
        self.assertEqual(result["specialized_review_link"], "/commands/ipconfig")
        self.assertEqual(
            result["review_workspace_link"],
            "/review?item=command_relationship_review%3AKCW-COMMAND",
        )

    def test_legacy_command_campaign_preview_and_reconciliation_are_bounded(self):
        service, planner, candidate, campaign_path, orchestration_path = (
            self.legacy_command_fixture()
        )
        before = {path: path.read_bytes() for path in (campaign_path, orchestration_path)}
        protected = {
            path: path.read_bytes()
            for path in (
                self.root / "app/decision_trees/network.json",
                self.root / "knowledge_base/commands/ipconfig.json",
                self.root / "knowledge_base/published/network-guide.json",
            )
        }

        preview = service.run(preview=True)

        self.assertEqual(preview.status, "SELECTED")
        self.assertEqual(preview.campaign["disposition"], "would_reconcile")
        self.assertEqual(preview.preparation["campaign_disposition"], "would_reconcile")
        self.assertEqual(preview.preparation["reconciliation"]["binding"], {
            key: candidate[key] for key in (
                "gap_identity", "workflow_id", "workflow_filename", "workflow_lifecycle",
                "node_id", "article_id", "command_identity",
            )
        })
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        result = service.run()

        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(result.campaign["campaign_id"], "KCP-LEGACY001")
        self.assertEqual(result.campaign["orchestration_id"], "KORCH-LEGACY001")
        self.assertEqual(result.campaign["disposition"], "reconciled")
        campaign = planner.get("KCP-LEGACY001")
        self.assertEqual(campaign["work_items"][0]["work_item_id"], "KCW-LEGACY001")
        for key in (
            "gap_identity", "workflow_id", "workflow_filename", "workflow_lifecycle",
            "node_id", "article_id", "command_identity",
        ):
            self.assertEqual(campaign["work_items"][0][key], candidate[key])
        self.assertEqual(
            [event["event"] for event in campaign["history"]].count(
                "command_relationship_handoff_reconciled"
            ),
            1,
        )
        self.assertEqual(campaign["history"][0]["event"], "created")
        self.assertEqual(len(list(campaign_path.parent.glob("KCP-*.json"))), 1)
        self.assertEqual(orchestration_path.read_bytes(), before[orchestration_path])
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)
        items = [item for item in ReviewWorkspaceService(self.root).items()
                 if item["item_type"] == "command_relationship_review"]
        self.assertEqual([item["key"] for item in items], [
            "command_relationship_review:KCW-LEGACY001"
        ])
        self.assertFalse(items[0]["decision_available"])

        service.run()
        repeated = planner.get("KCP-LEGACY001")
        self.assertEqual(
            [event["event"] for event in repeated["history"]].count(
                "command_relationship_handoff_reconciled"
            ),
            1,
        )
        self.assertEqual(len(list(campaign_path.parent.glob("KCP-*.json"))), 1)

    def test_live_shaped_complete_identities_reconcile_missing_declaration_handoff(self):
        service, planner, _, campaign_path, orchestration_path = self.legacy_command_fixture(
            identities_complete=True,
            article_declaration_absent=True,
        )
        review = ReviewWorkspaceService(self.root)
        before = {path: path.read_bytes() for path in (campaign_path, orchestration_path)}
        protected = {
            path: path.read_bytes()
            for path in (
                self.root / "app/decision_trees/network.json",
                self.root / "knowledge_base/commands/ipconfig.json",
                self.root / "knowledge_base/published/network-guide.json",
            )
        }
        status = review.command_relationship_review_status(
            "KCP-LEGACY001", "KCW-LEGACY001"
        )
        self.assertFalse(status["projectable"])
        self.assertEqual(status["reason"], "missing_relationship_declaration_handoff")
        self.assertEqual(review._command_relationship_items(), [])

        preview = service.run(preview=True)

        self.assertEqual(preview.campaign["disposition"], "would_reconcile")
        reconciliation = preview.preparation["reconciliation"]
        self.assertFalse(reconciliation["projectable_before"])
        self.assertEqual(
            reconciliation["missing_prerequisite"],
            "missing_relationship_declaration_handoff",
        )
        self.assertEqual(
            reconciliation["canonical_review_key"],
            "command_relationship_review:KCW-LEGACY001",
        )
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        result = service.run()

        self.assertEqual(result.campaign["disposition"], "reconciled")
        campaign = planner.get("KCP-LEGACY001")
        event = campaign["history"][-1]
        self.assertEqual(event["event"], "command_relationship_handoff_reconciled")
        self.assertEqual(event["changed_fields"], [
            "work_item.command_relationship_review_handoff"
        ])
        self.assertEqual(campaign["campaign_id"], "KCP-LEGACY001")
        self.assertEqual(campaign["gaps"][0]["gap_id"], "KCG-LEGACY001")
        self.assertEqual(campaign["work_items"][0]["work_item_id"], "KCW-LEGACY001")
        self.assertEqual(orchestration_path.read_bytes(), before[orchestration_path])
        items = ReviewWorkspaceService(self.root)._command_relationship_items()
        self.assertEqual([item["key"] for item in items], [
            "command_relationship_review:KCW-LEGACY001"
        ])
        self.assertFalse(items[0]["decision_available"])

        repeated = service.run()
        self.assertEqual(repeated.campaign["disposition"], "reused")
        self.assertEqual(
            [item["event"] for item in planner.get("KCP-LEGACY001")["history"]].count(
                "command_relationship_handoff_reconciled"
            ),
            1,
        )
        self.assertEqual(orchestration_path.read_bytes(), before[orchestration_path])
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)
        self.assertEqual(len(list(campaign_path.parent.glob("KCP-*.json"))), 1)

    def test_legacy_command_reconciliation_fails_closed_for_ambiguity_or_changed_gate(self):
        ambiguous, _, _, ambiguous_path, _ = self.legacy_command_fixture(
            duplicate_work=True
        )
        ambiguous_before = ambiguous_path.read_bytes()
        result = ambiguous.run()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("ambiguous", result.preparation["reason"].casefold())
        self.assertEqual(ambiguous_path.read_bytes(), ambiguous_before)

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        changed, _, _, changed_path, _ = self.legacy_command_fixture(state_overrides={
            "state": "complete", "action_authority": None, "next_action": None,
        })
        changed_before = changed_path.read_bytes()
        result = changed.run()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("human gate", result.preparation["reason"].casefold())
        self.assertEqual(changed_path.read_bytes(), changed_before)

    def test_completed_command_relationship_is_not_selected_for_reconciliation(self):
        service, planner, candidate, campaign_path, _ = self.legacy_command_fixture(
            reciprocal=True
        )
        planner.candidate = None
        planner.assess_stage2_candidates = lambda: []
        before = campaign_path.read_bytes()

        result = service.run()

        self.assertEqual(result.status, "NO-OP")
        self.assertEqual(campaign_path.read_bytes(), before)

    def test_preview_writes_nothing_and_matches_execute_selection(self):
        planner, orchestration = Planner(), Orchestration()
        service = self.service(planner, orchestration)
        preview = service.run(preview=True)
        self.assertEqual(planner.created, 0)
        self.assertEqual(orchestration.calls, [])
        executed = service.run()
        self.assertEqual(preview.selected_gap["gap_identity"],
                         executed.selected_gap["gap_identity"])

    def test_real_preview_does_not_create_runtime_directories_or_files(self):
        root = self.root / "read-only-preview"
        (root / "app/decision_trees").mkdir(parents=True)
        (root / "knowledge_base/commands").mkdir(parents=True)
        (root / "knowledge_base/published").mkdir(parents=True)
        taxonomy = root / "taxonomy.json"
        taxonomy.write_text(json.dumps({
            "schema_version": "1.0", "domains": [{
                "id": "windows-connectivity", "title": "Windows Connectivity",
                "category": "Networking", "platforms": ["Windows"],
                "areas": [{"id": "dns", "title": "DNS", "terms": ["dns"]}],
            }],
        }), encoding="utf-8")
        (root / "app/decision_trees/dns.json").write_text(json.dumps({
            "workflow_id": "dns", "name": "DNS", "category": "Networking",
            "platform": "Windows", "start_node": "inspect", "nodes": {
                "inspect": {"type": "instruction", "title": "Inspect DNS",
                            "instruction": "Inspect DNS evidence.", "next": "done"},
                "done": {"type": "resolution", "title": "Done"},
            },
        }), encoding="utf-8")
        planner = KnowledgeCoveragePlannerService(root, root / "campaigns", taxonomy)
        before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        result = AutonomousGrowthService(root, root / "campaigns", planner=planner).run(
            preview=True
        )
        after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(before, after)

    def test_one_campaign_created_and_existing_pipeline_reaches_human_gate(self):
        planner, orchestration = Planner(), Orchestration()
        result = self.service(planner, orchestration).run()
        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(planner.created, 1)
        self.assertEqual(result.campaign["disposition"], "created")
        self.assertEqual(result.preparation["outcome"], "prepared_for_human_review")
        self.assertEqual(result.validation["status"], "passed")
        self.assertTrue(result.human_review["required"])
        self.assertEqual(result.human_review["specialized_review_link"],
                         "/curator/growth/source-research/KRP-ONE")
        self.assertEqual(len([call for call in orchestration.calls if call[0] == "advance_item"]), 2)
        self.assertTrue(all(call[-1] == AUTONOMOUS_ACTOR for call in orchestration.calls))

    def test_active_equivalent_is_reused_without_duplicate(self):
        planner = Planner(campaigns=[analyzed_campaign()])
        result = self.service(planner, Orchestration()).run()
        self.assertEqual(planner.created, 0)
        self.assertEqual(result.campaign["campaign_id"], "KCP-EXISTING01")
        self.assertEqual(result.campaign["disposition"], "reused")

    def test_completed_equivalent_is_no_op(self):
        planner, orchestration = Planner(campaigns=[analyzed_campaign("completed")]), Orchestration()
        result = self.service(planner, orchestration).run()
        self.assertEqual(result.status, "NO-OP")
        self.assertEqual(planner.created, 0)
        self.assertEqual(orchestration.calls, [])

    def test_ambiguous_equivalent_campaign_identity_fails_closed(self):
        first, second = analyzed_campaign(), analyzed_campaign()
        second["campaign_id"] = "KCP-EXISTING02"
        result = self.service(Planner(campaigns=[first, second])).run()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("ambiguous", result.preparation["reason"].casefold())

    def test_unsupported_or_context_free_gap_is_no_op(self):
        projected = assessment(workflow_count=0)
        result = self.service(Planner(projected=projected)).run()
        self.assertEqual(result.status, "NO-OP")

    def test_pipeline_failure_stops_without_second_candidate(self):
        planner, orchestration = Planner(), Orchestration(fail=True)
        result = self.service(planner, orchestration).run()
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(len([call for call in orchestration.calls if call[0] == "advance_item"]), 1)
        self.assertIn("validation failed", result.preparation["reason"].casefold())

    def test_external_limit_stops_safely(self):
        result = self.service(max_external_operations=0).run()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("external-operation limit", result.preparation["reason"])

    def test_no_automatic_authority_is_reported(self):
        result = self.service().run()
        declarations = " ".join(result.intentional_non_actions)
        self.assertIn("No content or workflow was published", declarations)
        self.assertIn("No Growth lesson or proposal was approved", declarations)
        self.assertIn("No Curator task was resolved", declarations)
        self.assertIn("No repair was executed", declarations)
        source = Path("app/services/autonomous_growth_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)

    def test_run_does_not_touch_protected_state(self):
        protected = []
        for relative in (
            "app/workflow_drafts/sentinel.json",
            "app/workflow_publications/sentinel.json",
            "knowledge_base/published/sentinel.json",
            "curation_memory/memory.json",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel": true}', encoding="utf-8")
            protected.append((path, path.read_bytes()))
        self.service().run()
        self.assertEqual([(path, path.read_bytes()) for path, _ in protected], protected)

    def test_cli_reports_selected_no_op_and_blocked(self):
        scenarios = [
            ("SELECTED", 0), ("NO-OP", 0), ("BLOCKED", 2),
        ]
        for status, expected_code in scenarios:
            result = self.service().run(preview=True)
            object.__setattr__(result, "status", status)
            with self.subTest(status=status), patch(
                "app.services.autonomous_growth_service.AutonomousGrowthService"
            ) as service_type, patch("sys.stdout", new_callable=io.StringIO) as output:
                service_type.return_value.run.return_value = result
                code = main(["autonomous-growth", "--repository", str(self.root), "--preview"])
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(output.getvalue())["status"], status)


if __name__ == "__main__":
    unittest.main()
