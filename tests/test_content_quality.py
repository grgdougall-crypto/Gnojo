import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.app import app, available_workflows
from app.services.content_quality_service import ContentQualityService
from app.services.curator_content_quality_bridge_service import (
    CuratorContentQualityBridgeError,
    CuratorContentQualityBridgeService,
)
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class ContentQualityServiceTests(unittest.TestCase):
    def test_report_prioritizes_feedback_abandonment_and_coverage(self):
        workflows = {
            "slow": {
                "workflow_id": "slow", "name": "Slow Computer",
                "category": "Desktop Support", "platform": "Windows",
                "nodes": {
                    "start": {"type": "question", "question": "Slow?", "answers": {}},
                    "step": {"type": "instruction", "title": "Check", "instruction": "Check it."},
                    "done": {"type": "resolution", "title": "Done"},
                },
            }
        }
        records = [
            {"workflow_id": "slow", "status": "completed", "feedback": {"solved": "no", "clarity": 2, "confusing_step": "step"}},
            {"workflow_id": "slow", "status": "completed", "feedback": {"solved": "no", "clarity": 2, "confusing_step": "step"}},
            {"workflow_id": "slow", "status": "abandoned"},
        ]
        report = ContentQualityService().build(workflows, records, {"slow": "slow.json"})
        kinds = {item["kind"] for item in report["action_queue"]}
        self.assertTrue({"effectiveness", "clarity", "abandonment", "confusing_step", "knowledge", "learning"}.issubset(kinds))
        self.assertGreaterEqual(report["summary"]["high_priority"], 3)
        self.assertEqual(report["workflows"][0]["solved_rate"], 0)
        confusing = next(item for item in report["action_queue"] if item["kind"] == "confusing_step")
        self.assertEqual(confusing["node_id"], "step")
        self.assertEqual(confusing["filename"], "slow.json")
        self.assertEqual(confusing["quality_rule"], "CQ-FREQUENTLY-CONFUSING-STEP")
        self.assertEqual(confusing["report_count"], 2)
        self.assertEqual(confusing["sample_count"], 2)
        self.assertEqual(confusing["aggregate_clarity"], 2.0)

    def test_confusing_step_baseline_uses_authoritative_runtime_version(self):
        workflows = {
            "network": {
                "workflow_id": "network", "name": "Network",
                "nodes": {"dns": {"type": "instruction", "instruction": "Test DNS"}},
            }
        }
        records = [
            {"workflow_id": "network", "workflow_version": 1, "status": "completed",
             "feedback": {"solved": "no", "clarity": 1, "confusing_step": "dns"}},
            {"workflow_id": "network", "workflow_version": 2, "status": "completed",
             "feedback": {"solved": "no", "clarity": 3, "confusing_step": "dns"}},
            {"workflow_id": "network", "workflow_version": 2, "status": "completed",
             "feedback": {"solved": "yes", "clarity": 5, "confusing_step": None}},
        ]
        report = ContentQualityService().build(
            workflows, records, {"network": "network.json"},
            workflow_versions={"network": 2},
        )
        confusing = next(item for item in report["action_queue"] if item["kind"] == "confusing_step")
        self.assertEqual(confusing["workflow_version"], 2)
        self.assertEqual(confusing["report_count"], 1)
        self.assertEqual(confusing["sample_count"], 2)
        self.assertEqual(confusing["aggregate_clarity"], 4.0)


class CuratorContentQualityBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bridge = CuratorContentQualityBridgeService(self.root)
        self.item = {
            "kind": "confusing_step",
            "quality_rule": "CQ-FREQUENTLY-CONFUSING-STEP",
            "workflow_id": "windows_slow",
            "workflow_version": 4,
            "node_id": "confirm_windows",
            "priority": "high",
            "report_count": 3,
            "sample_count": 5,
            "aggregate_clarity": 2.6,
            "measured_at": "2026-08-19T20:00:00+00:00",
            "comment": "raw private feedback must never be copied",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_confusing_step_creates_one_stable_governed_task_without_content_changes(self):
        workflow = self.root / "app" / "decision_trees" / "windows_slow.json"
        publication = self.root / "app" / "workflow_publications" / "windows_slow" / "current.json"
        workflow.parent.mkdir(parents=True)
        publication.parent.mkdir(parents=True)
        workflow.write_text('{"unchanged": true}\n', encoding="utf-8")
        publication.write_text('{"unchanged": true}\n', encoding="utf-8")
        before = (workflow.read_bytes(), publication.read_bytes())

        first = self.bridge.send(self.item)
        second = self.bridge.send({**self.item, "report_count": 4})
        state = self.bridge.store.load()

        identity = "CQ-FREQUENTLY-CONFUSING-STEP|windows_slow|confirm_windows"
        expected_id = "GKT-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
        self.assertEqual(first["task_id"], expected_id)
        self.assertEqual(second["task_id"], expected_id)
        self.assertEqual(list(state["tasks"]), [expected_id])
        task = state["tasks"][expected_id]
        self.assertEqual(task["durable_identity"], identity)
        self.assertEqual(task["content_identifier"], "windows_slow:confirm_windows")
        self.assertEqual(task["related_workflows"], ["windows_slow"])
        self.assertEqual(task["execution_mode"], "HUMAN_DECISION")
        self.assertFalse(task["future_automated_fix"])
        self.assertEqual(task["quality_baseline"], {
            "workflow_id": "windows_slow",
            "workflow_version": 4,
            "node_id": "confirm_windows",
            "quality_rule": "CQ-FREQUENTLY-CONFUSING-STEP",
            "report_count": 4,
            "sample_count": 5,
            "aggregate_clarity": 2.6,
            "measured_at": "2026-08-19T20:00:00+00:00",
        })
        self.assertNotIn("raw private feedback", json.dumps(task))
        self.assertEqual((workflow.read_bytes(), publication.read_bytes()), before)
        self.assertFalse((self.root / "curation_memory" / "resolution_packages").exists())

    def test_unrelated_quality_finding_is_rejected_without_creating_a_task(self):
        with self.assertRaises(CuratorContentQualityBridgeError):
            self.bridge.send({**self.item, "kind": "clarity", "quality_rule": "CQ-CLARITY"})
        self.assertEqual(self.bridge.store.load()["tasks"], {})


class ContentQualityPageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = TroubleshootingHistoryService(Path(self.temporary.name))
        self.bridge = CuratorContentQualityBridgeService(Path(self.temporary.name) / "repository")
        self.previous_repository_root = app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        self.history_patch = patch("app.app.TroubleshootingHistoryService", return_value=self.history)
        self.bridge_patch = patch("app.app.CuratorContentQualityBridgeService", return_value=self.bridge)
        self.history_patch.start()
        self.bridge_patch.start()
        app.config.update(
            TESTING=True,
            STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(Path(self.temporary.name) / "repository"),
        )
        self.client = app.test_client()

    def tearDown(self):
        self.bridge_patch.stop()
        self.history_patch.stop()
        if self.previous_repository_root is None:
            app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = self.previous_repository_root
        self.temporary.cleanup()

    def test_content_studio_links_to_quality_dashboard(self):
        html = self.client.get("/content-studio").get_data(as_text=True)
        self.assertIn("Content Quality Dashboard", html)
        self.assertIn("Open Content Quality", html)
        self.assertIn('href="/content-quality"', html)

    def test_dashboard_renders_queue_health_and_coverage(self):
        response = self.client.get("/content-quality")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("What Needs Attention", html)
        self.assertIn("Library Overview", html)
        self.assertIn("Coverage Snapshot", html)
        self.assertIn("Open Workflow Studio", html)
        self.assertIn("Computer Running Slowly", html)
        self.assertIn("Knowledge coverage is thin", html)
        self.assertIn("Knowledge and Learning are content-derived coverage measures", html)
        self.assertIn("Percentage of instructional steps linked to supporting articles", html)
        self.assertIn("Percentage of questions and instructions with specific help text", html)
        self.assertIn("Open in Workflow Designer", html)
        self.assertIn("Run workflow", html)
        self.assertTrue(
            'aria-label="Run Higher-Layer Connectivity Diagnostics workflow"' in html
            or 'aria-label="Open Higher-Layer Connectivity Diagnostics in Workflow Designer"' in html
        )

    def test_editor_supports_direct_node_selection(self):
        html = self.client.get(
            "/workflow-editor/vpn_connectivity_win.json?node=instr_check_adapter_status"
        ).get_data(as_text=True)
        self.assertIn('data-node-id="instr_check_adapter_status"', html)
        self.assertIn("requestedNodeId", html)

    def test_confusing_step_has_distinct_open_and_curator_actions_then_shows_tracked_state(self):
        version = available_workflows()["windows_slow"].get("version")
        for clarity, comment in ((2, "private first comment"), (3, "private second comment")):
            record = self.history.start("windows_slow", "Computer Running Slowly", "confirm_windows", version=version)
            self.history.complete(record["id"], "confirm_windows")
            self.history.add_feedback(record["id"], {
                "solved": "no", "clarity": clarity,
                "confusing_step": "confirm_windows", "comment": comment,
            })

        before = self.client.get("/content-quality").get_data(as_text=True)
        self.assertIn("Send to Curator", before)
        self.assertIn("Open affected step", before)
        self.assertIn(
            'aria-label="Open Computer Running Slowly affected step in Workflow Designer"',
            before,
        )
        self.assertIn("/workflow-editor/windows_slow.json?node=confirm_windows", before)
        self.assertIn("return_to=/content-quality", before)

        editor = self.client.get(
            "/workflow-editor/windows_slow.json?node=confirm_windows&return_to=%2Fcontent-quality"
        ).get_data(as_text=True)
        self.assertIn('href="/content-quality"', editor)
        self.assertIn('aria-label="Back to Content Quality"', editor)

        response = self.client.post(
            "/content-quality/confusing-step/curator",
            data={"workflow_id": "windows_slow", "node_id": "confirm_windows"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Tracked by Curator", html)
        self.assertNotIn("Send to Curator", html)
        self.assertIn("/workflow-editor/windows_slow.json?node=confirm_windows", html)
        task = next(iter(self.bridge.store.load()["tasks"].values()))
        tracked_url = (
            f"/curator/tasks/{task['task_id']}?origin=content_quality"
            "&amp;return_to=/content-quality%23queueTitle"
        )
        self.assertIn(tracked_url, html)
        self.assertIn(
            'aria-label="Open tracked Curator task and return to Content Quality"', html,
        )
        detail = self.client.get(
            f"/curator/tasks/{task['task_id']}"
            "?origin=content_quality&return_to=/content-quality%23queueTitle"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Return to Content Quality", detail.data)
        self.assertIn(b'href="/content-quality#queueTitle"', detail.data)
        self.assertNotIn("private first comment", json.dumps(task))
        self.assertNotIn("private second comment", json.dumps(task))

    def test_dashboard_and_curator_bridge_use_production_feedback_only(self):
        version = available_workflows()["windows_slow"].get("version")

        def feedback(environment, clarity):
            record = self.history.start(
                "windows_slow", "Computer Running Slowly", "confirm_windows",
                version=version, session_environment=environment,
            )
            self.history.complete(record["id"], "confirm_windows")
            self.history.add_feedback(record["id"], {
                "solved": "no", "clarity": clarity,
                "confusing_step": "confirm_windows", "comment": "private",
            })

        feedback("development", 1)
        feedback("test", 1)
        before_threshold = self.client.get("/content-quality").get_data(as_text=True)
        self.assertNotIn("Send to Curator", before_threshold)

        feedback("production", 2)
        eligible = self.client.get("/content-quality").get_data(as_text=True)
        self.assertIn("Send to Curator", eligible)
        response = self.client.post(
            "/content-quality/confusing-step/curator",
            data={"workflow_id": "windows_slow", "node_id": "confirm_windows"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        task = next(iter(self.bridge.store.load()["tasks"].values()))
        self.assertEqual(task["quality_baseline"]["report_count"], 1)
        self.assertEqual(task["quality_baseline"]["sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
