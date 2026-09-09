import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.data_root import APPLICATION_ROOT
from app.services.batch_propagation_autopilot_service import (
    BatchPropagationAutopilotError,
    BatchPropagationAutopilotService,
)
from curator.__main__ import main


def candidate(index, gap_type="weak_learning_coverage", workflow=None):
    workflow = workflow or f"workflow-{index}"
    return {
        "gap_identity": f"workflow:{workflow}:{gap_type}", "gap_type": gap_type,
        "workflow_id": workflow, "workflow_filename": f"{workflow}.json",
        "area_id": workflow, "area_title": workflow.replace("-", " ").title(),
        "title": f"Improve {workflow}", "selection_explanation": f"Ranked {index}",
        "ranking": {"stable_tiebreaker": f"{index:02d}"},
    }


class Result:
    def __init__(self, payload): self.payload = payload
    def as_dict(self): return deepcopy(self.payload)


class Planner:
    def domains(self): return [{"id": "desktop-support", "title": "Desktop Support"}]


class Growth:
    def __init__(self, candidates, outcomes=None):
        self.planner = Planner(); self.candidates = candidates
        self.outcomes = outcomes or {}; self.calls = []

    def ranked_candidates(self, domain):
        self.calls.append(("rank", domain)); return deepcopy(self.candidates)

    def prepare_ranked_candidate(self, value, *, preview=False):
        identity = value["gap_identity"]; self.calls.append(("prepare", identity, preview))
        if preview:
            return Result({"status": "SELECTED", "campaign": {"disposition": "would_create"},
                           "preparation": {"outcome": "preview"}, "human_review": {}})
        configured = self.outcomes.get(identity)
        if isinstance(configured, list):
            payload = configured.pop(0) if len(configured) > 1 else configured[0]
        else:
            payload = configured
        return Result(payload or {"status": "NO-OP", "campaign": {"disposition": "completed_equivalent"},
                                  "preparation": {"outcome": "already_prepared"},
                                  "human_review": {"required": False}})


class Learning:
    def __init__(self, previews=None, results=None):
        self.previews = list(previews or [{"eligible_count": 0, "completion_ready": True,
                                          "skipped_nodes": []}])
        self.results = list(results or []); self.prepare_calls = 0

    def preview(self, *args): return deepcopy(self.previews[min(self.prepare_calls, len(self.previews)-1)])
    def prepare(self, *args, actor):
        value = self.results[min(self.prepare_calls, len(self.results)-1)]
        self.prepare_calls += 1; return deepcopy(value)


class Research:
    def __init__(self, packages=None): self.packages = packages or []
    def list_for_campaign(self, campaign_id): return deepcopy(self.packages)


class SourceAutopilot:
    def __init__(self, snapshot=None): self.snapshot = snapshot or {}
    def current_snapshot(self, package_id): return deepcopy(self.snapshot)


class BatchPropagationAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self): self.temporary.cleanup()

    def service(self, values, *, outcomes=None, learning=None, research=None, source=None):
        growth = Growth(values, outcomes)
        service = BatchPropagationAutopilotService(
            self.root, growth=growth, learning=learning or Learning(),
            research=research or Research(), source_autopilot=source or SourceAutopilot(),
            now=lambda: "2026-09-09T12:00:00+00:00",
        )
        return service, growth

    def test_preview_is_write_free_deterministic_and_selects_unique_workflows(self):
        values = [candidate(0), candidate(1),
                  candidate(2, "missing_article", workflow="workflow-1")]
        service, _ = self.service(values)
        before = list(self.root.rglob("*"))
        first = service.preview(domain="Desktop Support", limit=3)
        second = service.preview(domain="desktop-support", limit=3)
        self.assertEqual(first["selected_count"], 2)
        self.assertEqual([x["gap_identity"] for x in first["items"]],
                         [x["gap_identity"] for x in second["items"]])
        self.assertEqual(before, list(self.root.rglob("*")))

    def test_limit_one_to_ten_is_enforced(self):
        service, _ = self.service([candidate(0)])
        for value in (0, 11):
            with self.assertRaisesRegex(BatchPropagationAutopilotError, "between 1 and 10"):
                service.preview(domain="Desktop Support", limit=value)

    def test_selection_is_bounded_to_ten_and_supported_types(self):
        types = ["missing_article", "weak_learning_coverage",
                 "missing_command_reference", "missing_workflow"]
        values = [candidate(index, types[index % len(types)]) for index in range(12)]
        service, _ = self.service(values)
        result = service.preview(domain="Desktop Support", limit=10)
        self.assertEqual(result["selected_count"], 10)
        self.assertEqual([item["gap_identity"] for item in result["items"]],
                         [item["gap_identity"] for item in values[:10]])

    def test_unsupported_candidate_fails_closed_without_preparation(self):
        value = candidate(0); value["gap_type"] = "unsupported"
        service, growth = self.service([value])
        with self.assertRaisesRegex(BatchPropagationAutopilotError, "unsupported"):
            service.preview(domain="Desktop Support", limit=1)
        self.assertFalse(any(call[0] == "prepare" for call in growth.calls))

    def test_multiple_workflows_progress_independently_and_failure_is_contained(self):
        values = [candidate(0), candidate(1), candidate(2)]
        outcomes = {
            values[1]["gap_identity"]: {"status": "BLOCKED", "campaign": {},
                "preparation": {"reason": "Identity is stale."}, "human_review": {}},
        }
        service, _ = self.service(values, outcomes=outcomes)
        result = service.run(domain="Desktop Support", limit=3)
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual([x["state"] for x in result["items"]],
                         ["MACHINE_COMPLETE", "BLOCKED", "MACHINE_COMPLETE"])

    def test_safe_learning_generation_retries_twice_then_stops(self):
        value = candidate(0)
        outcome = {"status": "SELECTED", "campaign": {"campaign_id": "KCP-1",
            "orchestration_id": "KORCH-1", "work_item_id": "KCW-1", "disposition": "created"},
            "preparation": {"outcome": "prepared_for_human_review", "artifacts": []},
            "human_review": {"required": True, "action": "author_learning_content",
                             "specialized_review_link": "/learning"}}
        failed = {"status": "failed", "generated_saved": 0,
                  "remaining_blank_eligible_nodes": ["n1"],
                  "skipped_failed_nodes": [{"node_id": "n1", "reason": "provider unavailable"}],
                  "skipped_nodes": [], "completion_ready": False}
        learning = Learning(previews=[{"eligible_count": 1, "completion_ready": False,
                                       "skipped_nodes": []}], results=[failed, failed, failed])
        service, _ = self.service([value], outcomes={value["gap_identity"]: outcome},
                                  learning=learning)
        result = service.run(domain="Desktop Support", limit=1)
        self.assertEqual(learning.prepare_calls, 3)
        self.assertEqual(len(result["items"][0]["ai_retry_provenance"]), 3)
        self.assertEqual(result["items"][0]["state"], "FAILED_SAFE")

    def test_ambiguous_source_enters_exception_queue_and_snapshot_is_reused(self):
        value = candidate(0, "missing_article")
        outcome = {"status": "SELECTED", "campaign": {"campaign_id": "KCP-1",
            "orchestration_id": "KORCH-1", "work_item_id": "KCW-1", "disposition": "reuse"},
            "preparation": {"outcome": "prepared_for_human_review", "artifacts": []},
            "human_review": {"required": True, "action": "approve_source",
                             "specialized_review_link": "/autopilot"}}
        research = Research([{"package_id": "KRP-1", "work_item_id": "KCW-1"}])
        source = SourceAutopilot({"human_decision_count": 2, "curated_count": 7,
                                  "approval_blockers": []})
        service, _ = self.service([value], outcomes={value["gap_identity"]: outcome},
                                  research=research, source=source)
        result = service.run(domain="Desktop Support", limit=1)
        self.assertEqual(result["items"][0]["state"], "HUMAN_EXCEPTION")
        self.assertEqual(result["items"][0]["review_url"],
                         "/curator/growth/source-research/KRP-1/autopilot")

    def test_resume_reuses_batch_without_duplicate_batch_or_approval(self):
        service, growth = self.service([candidate(0)])
        first = service.run(domain="Desktop Support", limit=1)
        second = service.run(domain="Desktop Support", limit=1)
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(len(list(service.batch_root.glob("KPB-*.json"))), 1)
        self.assertFalse(any("approve" in str(call).casefold() for call in growth.calls))
        self.assertFalse(second["authority"]["publication"])
        self.assertFalse(second["authority"]["command_execution"])
        self.assertEqual(len(second["history"]), len(first["history"]))

    def test_control_center_get_is_read_only(self):
        service, _ = self.service([candidate(0)])
        result = service.run(domain="Desktop Support", limit=1)
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        flask_app.config.update(TESTING=True)
        with patch("app.app.BatchPropagationAutopilotService", return_value=service):
            response = flask_app.test_client().get(result["control_center_url"])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Batch Propagation", response.data)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})

    def test_resume_fails_closed_for_persisted_selection_outside_bounds(self):
        service, _ = self.service([candidate(0)])
        result = service.run(domain="Desktop Support", limit=1)
        path = service._path(result["batch_id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["selection"] = [candidate(index) for index in range(11)]
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BatchPropagationAutopilotError, "bounds"):
            service.run(batch_id=result["batch_id"])

    def test_cli_preview_dispatches_without_writes(self):
        output = io.StringIO()
        payload = {"status": "PREVIEW", "preview": True, "items": [],
                   "selected_count": 0}
        with patch.object(
            BatchPropagationAutopilotService, "__init__", return_value=None
        ), patch.object(
            BatchPropagationAutopilotService, "preview", return_value=payload
        ), redirect_stdout(output):
            code = main(["propagate-library", "--repository", str(self.root),
                         "--domain", "Desktop Support", "--limit", "10", "--preview"])
        self.assertEqual(code, 0)
        self.assertIn('"preview": true', output.getvalue())
        self.assertFalse((self.root / "knowledge_campaigns" / "propagation_batches").exists())

    def test_configured_data_root_contains_batch_state(self):
        with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(self.root)}):
            service = BatchPropagationAutopilotService(
                growth=Growth([candidate(0)]), learning=Learning(),
                research=Research(), source_autopilot=SourceAutopilot(),
            )
        self.assertEqual(service.batch_root, self.root / "knowledge_campaigns" / "propagation_batches")

    def test_configured_data_root_keeps_immutable_taxonomy_source_relative(self):
        with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(self.root)}):
            service = BatchPropagationAutopilotService()
        self.assertEqual(service.batch_root,
                         self.root / "knowledge_campaigns" / "propagation_batches")
        self.assertEqual(
            service.growth.planner.taxonomy_path.resolve(),
            (APPLICATION_ROOT / "app" / "data" / "knowledge_coverage_taxonomy.json").resolve(),
        )


if __name__ == "__main__": unittest.main()
