from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.services.autonomous_growth_service import (
    SUPPORTED_GAP_TYPES,
    AutonomousGrowthResult,
    AutonomousGrowthService,
)
from app.services.supervised_library_growth_batch_service import (
    SupervisedLibraryGrowthBatchService,
)
from curator.__main__ import main


def candidate(position: int, gap_type: str = "missing_article") -> dict:
    identity = f"desktop-support:topic-{position}:{gap_type}"
    return {
        "gap_identity": identity,
        "gap_type": gap_type,
        "domain_id": "desktop-support",
        "intended_artifact": {
            "missing_article": "knowledge_article",
            "weak_learning_coverage": "learning_content_plan",
            "missing_command_reference": "command_relationship_review",
            "missing_workflow": "workflow_draft",
        }.get(gap_type, "unknown"),
        "selection_explanation": f"Ranked candidate {position} from deterministic evidence.",
        "ranking": {
            "type_priority": position,
            "evidence_strength": 3,
            "coverage_deficiency": 10,
            "runtime_relevance": 0,
            "workflow_context": 1,
            "relevant_nodes": 1,
            "stable_tiebreaker": identity,
        },
    }


class Planner:
    def domains(self):
        return [{"id": "desktop-support", "title": "Desktop Support"}]


class Growth:
    def __init__(self, candidates):
        self.planner = Planner()
        self.candidates = deepcopy(candidates)
        self.calls = []
        self.campaigns = {}

    def ranked_candidates(self, domain_id):
        self.calls.append(("rank", domain_id))
        return deepcopy(self.candidates)

    def prepare_ranked_candidate(self, selected, *, preview=False):
        identity = selected["gap_identity"]
        disposition = "reuse" if identity in self.campaigns else "would_create"
        if not preview and identity not in self.campaigns:
            self.campaigns[identity] = f"KCP-{len(self.campaigns) + 1}"
            disposition = "created"
        self.calls.append(("prepare", identity, preview))
        campaign_id = self.campaigns.get(identity)
        return AutonomousGrowthResult(
            status="SELECTED",
            preview=preview,
            selected_gap=deepcopy(selected),
            selection_explanation=selected["selection_explanation"],
            campaign={"campaign_id": campaign_id, "disposition": disposition},
            preparation={
                "outcome": "preview" if preview else "prepared_for_human_review"
            },
            validation={
                "status": "not_run" if preview else "passed",
                "basis": "existing_pipeline_projection",
            },
            human_review={
                "required": not preview,
                "review_workspace_link": "/review",
                "campaign_control_link": "/curator/growth/coverage-campaigns/example/orchestration",
            },
            intentional_non_actions=(
                "No content or workflow was published.",
                "No Growth lesson or proposal was approved.",
            ),
        )


class SupervisedLibraryGrowthBatchTests(unittest.TestCase):
    def service(self, values):
        growth = Growth(values)
        return SupervisedLibraryGrowthBatchService(growth=growth), growth

    def test_deterministic_top_three_and_mixed_supported_types(self):
        values = [
            candidate(0, "missing_article"),
            candidate(1, "weak_learning_coverage"),
            candidate(2, "missing_command_reference"),
            candidate(3, "missing_workflow"),
        ]
        service, _ = self.service(values)
        first = service.run(domain="Desktop Support", preview=True)
        second = service.run(domain="desktop-support", preview=True)
        self.assertEqual(first.selected_count, 3)
        self.assertEqual(
            [item["gap_identity"] for item in first.items],
            [item["gap_identity"] for item in second.items],
        )
        self.assertEqual(
            [item["gap_type"] for item in first.items],
            ["missing_article", "weak_learning_coverage", "missing_command_reference"],
        )

    def test_desktop_support_is_a_configured_batch_domain(self):
        growth = AutonomousGrowthService(Path.cwd())
        domains = {item["id"]: item for item in growth.planner.domains()}
        self.assertTrue(domains["desktop-support"]["batch_only"])
        ranked = growth.ranked_candidates("desktop-support")
        self.assertTrue(ranked)
        self.assertTrue(all(item["domain_id"] == "desktop-support" for item in ranked))
        self.assertTrue(all(item["gap_type"] in SUPPORTED_GAP_TYPES for item in ranked))

    def test_preview_is_read_only_and_describes_creation(self):
        service, growth = self.service([candidate(0), candidate(1)])
        result = service.run(domain="Desktop Support", limit=2, preview=True)
        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(growth.campaigns, {})
        self.assertTrue(all(item["existing_work_disposition"] == "would_create"
                            for item in result.items))
        self.assertTrue(all(item["outcome"]["validation"]["status"] == "not_run"
                            for item in result.items))

    def test_execution_is_bounded_and_rerun_reuses_existing_work(self):
        service, growth = self.service([candidate(index) for index in range(5)])
        first = service.run(domain="Desktop Support", limit=3)
        second = service.run(domain="Desktop Support", limit=3)
        self.assertEqual(first.selected_count, 3)
        self.assertEqual(len(growth.campaigns), 3)
        self.assertTrue(all(item["existing_work_disposition"] == "created"
                            for item in first.items))
        self.assertTrue(all(item["existing_work_disposition"] == "reuse"
                            for item in second.items))
        self.assertTrue(all(item["outcome"]["human_review"]["required"]
                            for item in first.items))
        self.assertTrue(all("published" in note.casefold()
                            for note in first.intentional_non_actions[:1]))

    def test_unsupported_or_duplicate_candidate_fails_closed_without_preparation(self):
        for values in (
            [candidate(0, "unsupported")],
            [candidate(0), candidate(0)],
        ):
            service, growth = self.service(values)
            result = service.run(domain="Desktop Support")
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.selected_count, 0)
            self.assertFalse(any(call[0] == "prepare" for call in growth.calls))

    def test_domain_and_limit_fail_closed(self):
        service, growth = self.service([candidate(0)])
        self.assertEqual(service.run(domain="Other").status, "BLOCKED")
        self.assertEqual(service.run(domain="Desktop Support", limit=4).status, "BLOCKED")
        self.assertFalse(any(call[0] == "prepare" for call in growth.calls))

    def test_cli_dispatches_preview_without_approval_or_publication(self):
        service, _ = self.service([candidate(0)])
        output = io.StringIO()
        with patch(
            "app.services.supervised_library_growth_batch_service."
            "SupervisedLibraryGrowthBatchService",
            return_value=service,
        ), redirect_stdout(output):
            code = main([
                "grow-library", "--domain", "Desktop Support", "--limit", "3", "--preview"
            ])
        self.assertEqual(code, 0)
        self.assertIn('"preview": true', output.getvalue())
        self.assertIn('"selected_count": 1', output.getvalue())


if __name__ == "__main__":
    unittest.main()
