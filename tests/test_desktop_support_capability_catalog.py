import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.data_root import APPLICATION_ROOT
from app.services.autonomous_growth_service import AutonomousGrowthService
from app.services.batch_propagation_autopilot_service import (
    BatchPropagationAutopilotService,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerService,
)


class DesktopSupportCapabilityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for path in (
            "app/decision_trees", "app/workflow_drafts", "app/workflow_publications",
            "knowledge_base/drafts", "knowledge_base/published", "knowledge_base/archive",
            "knowledge_base/commands", "knowledge_base/scripts",
        ):
            (self.root / path).mkdir(parents=True)
        taxonomy = {
            "schema_version": "1.0",
            "domains": [{
                "id": "desktop-support", "title": "Desktop Support",
                "category": "Desktop Support", "platforms": ["Windows"],
                "batch_only": True, "capability_catalog": "capabilities.json",
                "areas": [],
            }],
        }
        catalog = {
            "schema_version": "1.0", "catalog_id": "test-desktop-capabilities",
            "domain_id": "desktop-support", "title": "Test capabilities",
            "capabilities": [
                self.capability("exact-topic", ["workflow"], workflow=["intended"]),
                self.capability("missing-topic", ["workflow", "article"]),
                self.capability("article-topic", ["workflow", "article"],
                                workflow=["intended"], article=["intended-guide"]),
                self.capability("command-topic", ["command_reference"]),
            ],
        }
        self.taxonomy_path = self.root / "taxonomy.json"
        self.taxonomy_path.write_text(json.dumps(taxonomy), encoding="utf-8")
        (self.root / "capabilities.json").write_text(
            json.dumps(catalog), encoding="utf-8"
        )
        self.write_workflow("loose", "Exact Topic Troubleshooting")
        self.service = KnowledgeCoveragePlannerService(
            self.root, self.root / "knowledge_campaigns", self.taxonomy_path
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def capability(identifier, expected, *, workflow=None, article=None, command=None):
        relationships = []
        if "workflow" in expected and "article" in expected:
            relationships.append("workflow_article")
        if "article" in expected and "command_reference" in expected:
            relationships.append("article_command_reference")
        return {
            "id": identifier, "title": identifier.replace("-", " ").title(),
            "level": "core", "platform": "Windows", "category": "Test",
            "terms": ["exact topic", identifier.replace("-", " ")],
            "expected_artifacts": expected,
            "likely_relationships": relationships,
            "artifact_matches": {
                "workflow": workflow or [], "article": article or [],
                "command": command or [],
            },
        }

    def write_workflow(self, identifier, name):
        value = {
            "workflow_id": identifier, "name": name, "category": "Desktop Support",
            "platform": "Windows", "start_node": "start",
            "nodes": {"start": {"type": "resolution", "title": "Done"}},
        }
        (self.root / "app" / "decision_trees" / f"{identifier}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_code_owned_catalog_loads_deterministically(self):
        planner = KnowledgeCoveragePlannerService(APPLICATION_ROOT)
        first = planner.capability_catalog("desktop-support")
        second = planner.capability_catalog("desktop-support")
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first["capabilities"]), 50)
        self.assertEqual(len(first["capabilities"]), len({
            item["id"] for item in first["capabilities"]
        }))

    def test_exact_allowlist_mapping_rejects_loose_related_content(self):
        first = self.service.assess_domain("desktop-support")
        exact = next(item for item in first["areas"]
                     if item["capability_id"] == "exact-topic")
        self.assertFalse(exact["artifact_facets"]["workflow"])
        self.write_workflow("intended", "Different Display Name")
        second = self.service.assess_domain("desktop-support")
        exact = next(item for item in second["areas"]
                     if item["capability_id"] == "exact-topic")
        self.assertTrue(exact["artifact_facets"]["workflow"])

    def test_supported_gaps_and_identities_are_stable(self):
        self.write_workflow("intended", "Intended Workflow")
        first = self.service.assess_domain("desktop-support")
        second = self.service.assess_domain("desktop-support")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["gaps"], second["gaps"])
        gaps = {(item["capability_id"], item["gap_type"]): item
                for item in first["gaps"]}
        self.assertIn(("missing-topic", "missing_workflow"), gaps)
        self.assertIn(("article-topic", "missing_article"), gaps)
        self.assertIn(("command-topic", "missing_command_reference"), gaps)
        self.assertEqual(
            gaps[("missing-topic", "missing_workflow")]["gap_identity"],
            "capability:desktop-support:windows:missing-topic:missing_workflow",
        )

    def test_growth_operations_and_ranking_use_catalog_without_writes(self):
        self.write_workflow("intended", "Intended Workflow")
        growth = AutonomousGrowthService(self.root, planner=self.service)
        ranked = growth.ranked_candidates("desktop-support")
        self.assertTrue(any(
            item["gap_type"] == "missing_workflow"
            and item["capability_id"] == "missing-topic"
            for item in ranked
        ))
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        operations = BatchPropagationAutopilotService(
            self.root, growth=growth, learning=object(), research=object(),
            source_autopilot=object(),
        ).operations(domain="Desktop Support", limit=3)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(operations["capability_coverage"]["total"], 4)
        self.assertGreaterEqual(operations["counts_by_gap_type"]["missing_workflow"], 1)
        self.assertGreaterEqual(operations["counts_by_gap_type"]["missing_article"], 1)
        self.assertGreaterEqual(
            operations["counts_by_gap_type"]["missing_command_reference"], 1
        )
        self.assertTrue(operations["next_candidates"])
        self.assertTrue(all(
            item["gap_identity"].startswith("capability:desktop-support:windows:")
            for item in operations["next_candidates"]
        ))

    def test_capability_campaign_is_scoped_to_the_selected_gap(self):
        growth = AutonomousGrowthService(self.root, planner=self.service)
        selected = next(
            item for item in growth.ranked_candidates("desktop-support")
            if item["gap_type"] == "missing_workflow"
            and item["capability_id"] == "missing-topic"
        )
        campaign = self.service.create(
            title="Missing Topic Coverage", domain_id="desktop-support",
            objective="Prepare governed evidence and a workflow draft.",
            actor="Autonomous Growth Stage 2",
            metadata={
                "initiated_by": "autonomous_growth_stage2",
                "gap_identity": selected["gap_identity"],
                "selection_basis": selected["selection_explanation"],
                "assessment_fingerprint": selected["assessment_fingerprint"],
                "selected_gap": selected,
            },
        )
        analyzed = self.service.analyze(campaign["campaign_id"])
        self.assertEqual(len(analyzed["gaps"]), 1)
        self.assertEqual(analyzed["gaps"][0]["gap_identity"], selected["gap_identity"])
        self.assertEqual(len(analyzed["work_items"]), 1)
        self.assertEqual(analyzed["work_items"][0]["work_type"], "workflow")

    def test_real_catalog_missing_article_reaches_research_without_external_call(self):
        self.write_workflow("application_crash", "Application Crashes")
        planner = KnowledgeCoveragePlannerService(
            self.root,
            self.root / "knowledge_campaigns",
            APPLICATION_ROOT / "app" / "data" / "knowledge_coverage_taxonomy.json",
        )
        with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(self.root)}):
            growth = AutonomousGrowthService(
                self.root, planner=planner, max_external_operations=0
            )
            selected = next(
                item for item in growth.ranked_candidates("desktop-support")
                if item["gap_type"] == "missing_article"
            )
            result = growth.prepare_ranked_candidate(selected).as_dict()

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("external-operation limit", result["preparation"]["reason"])
        self.assertEqual(
            [item["action"] for item in result["preparation"]["artifacts"]],
            ["prepare_research"],
        )
        self.assertNotIn(
            "Phase 2 research requires", result["preparation"]["reason"]
        )

    def test_growth_operations_renders_capability_summary_read_only(self):
        self.write_workflow("intended", "Intended Workflow")
        service = BatchPropagationAutopilotService(
            self.root,
            growth=AutonomousGrowthService(self.root, planner=self.service),
            learning=object(), research=object(), source_autopilot=object(),
        )
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        flask_app.config.update(TESTING=True)
        with patch("app.app.BatchPropagationAutopilotService", return_value=service):
            response = flask_app.test_client().get(
                "/curator/growth/operations?domain=desktop-support&limit=3"
            )
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Authoritative capability inventory", response.data)
        self.assertIn(b"4 total", response.data)
        self.assertIn(b"Browse capability coverage by category", response.data)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
