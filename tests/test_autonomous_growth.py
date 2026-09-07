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
    def __init__(self, *, campaigns=None, projected=None):
        self.campaigns = deepcopy(campaigns or [])
        self.projected = deepcopy(projected or assessment())
        self.created = 0
        self.analyzed = 0

    def domains(self):
        return [{"id": "windows-connectivity"}]

    def assess_domain(self, domain_id):
        return deepcopy(self.projected)

    def list_campaigns(self):
        return deepcopy(self.campaigns)

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
        campaign["gaps"] = [{
            "gap_id": "KCG-CAMPAIGN", "gap_type": "missing_article", "area_id": "dns"
        }]
        campaign["work_items"] = [{
            "work_item_id": "KCW-DNS", "gap_id": "KCG-CAMPAIGN",
            "work_type": "knowledge_article",
        }]
        return deepcopy(campaign)


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


def analyzed_campaign(status="analyzed"):
    return {
        "campaign_id": "KCP-EXISTING01", "domain": "windows-connectivity",
        "status": status, "last_analyzed_at": "now",
        "creation_metadata": {"gap_identity": "windows-connectivity:dns:missing_article"},
        "gaps": [{"gap_id": "KCG-OLD", "gap_type": "missing_article", "area_id": "dns"}],
        "work_items": [{"work_item_id": "KCW-DNS", "gap_id": "KCG-OLD",
                        "work_type": "knowledge_article"}],
    }


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

    def test_preview_writes_nothing_and_matches_execute_selection(self):
        planner, orchestration = Planner(), Orchestration()
        service = self.service(planner, orchestration)
        preview = service.run(preview=True)
        self.assertEqual(planner.created, 0)
        self.assertEqual(orchestration.calls, [])
        executed = service.run()
        self.assertEqual(preview.selected_gap["gap_identity"],
                         executed.selected_gap["gap_identity"])

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
