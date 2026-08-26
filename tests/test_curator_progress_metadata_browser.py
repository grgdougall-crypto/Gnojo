import json
import re
import tempfile
import unittest
from pathlib import Path

from app.app import app as flask_app
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_repair_planner import CuratorRepairPlanner
from curator.memory import CuratorMemoryStore


class CuratorProgressMetadataBrowserTests(unittest.TestCase):
    task_id = "GKT-PROGRESS-BROWSER"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.drafts.mkdir(parents=True)
        self.filename = "higher_layer_connectivity.json"
        self.path = self.drafts / self.filename
        self.path.write_bytes(self.workflow_bytes(self.workflow()))
        self.before = self.path.read_bytes()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"][self.task_id] = self.task()
        self.store.save(state)
        queue = CuratorRepairPlanner(self.root).build(self.empty_integrity_report())
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["affected_content"]["task_id"], self.task_id)
        self.assertEqual(queue[0]["classification"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(
            queue[0]["affected_content"]["structural_adapter_id"],
            "branch_aware_progress_metadata",
        )
        self.item_id = queue[0]["item_id"]
        self.fix_session = CuratorFixSessionService(self.root).create(
            started_by="Stage 3.7.1 Reviewer",
            originating_audit_id=None,
            queue=queue,
            baseline={"counts": {}},
        )
        self.publication_before = self.publication_state()
        self.previous_root = flask_app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        flask_app.config.update(
            TESTING=True,
            STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root),
        )
        self.client = flask_app.test_client()

    def tearDown(self):
        if self.previous_root is None:
            flask_app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            flask_app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = self.previous_root
        self.temporary.cleanup()

    @staticmethod
    def workflow():
        nodes = {}
        for step in range(1, 6):
            destination = f"step_{step + 1}" if step < 5 else "done"
            nodes[f"step_{step}"] = {
                "type": "question",
                "question": f"Check {step}?",
                "answers": {"yes": {"label": "Yes", "next": destination}},
            }
        nodes["done"] = {
            "type": "resolution",
            "title": "Done",
            "message": "Review complete.",
        }
        return {
            "workflow_id": "higher_layer_connectivity",
            "name": "Higher-Layer Connectivity Diagnostics",
            "category": "Networking",
            "platform": "Cross-platform",
            "estimated_steps": 4,
            "start_node": "step_1",
            "nodes": nodes,
        }

    @classmethod
    def task(cls):
        return {
            "task_id": cls.task_id,
            "finding_id": "CUR-PROGRESS-BROWSER",
            "status": "open",
            "owner": "Curator",
            "priority": "Medium",
            "classification": "Risk",
            "confidence": "high",
            "knowledge_debt_score": 5,
            "times_observed": 1,
            "first_seen": "2026-08-25T00:00:00+00:00",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "title": "Workflow progress estimate conflicts with valid paths",
            "explanation": "Static progress can complete before the selected route.",
            "recommended_action": "Review the governed branch-aware progress repair.",
            "durable_identity": "progress|higher_layer_connectivity",
            "curator_rule": "CUR-WR-PROGRESS",
            "finding_type": "workflow_reasoning_progress_inconsistency",
            "content_type": "workflow",
            "content_identifier": "higher_layer_connectivity",
            "related_workflows": ["higher_layer_connectivity"],
            "history": [],
            "evidence": [],
            "structured_evidence": {
                "configured_steps": 4,
                "maximum_user_visible_nodes": 6,
            },
        }

    @staticmethod
    def workflow_bytes(workflow):
        return (json.dumps(workflow, indent=4, ensure_ascii=False) + "\n").encode("utf-8")

    def publication_state(self):
        root = self.root / "app" / "workflow_publications"
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()
        } if root.exists() else {}

    @staticmethod
    def empty_integrity_report():
        return {
            "broken_relationships": [],
            "duplicate_groups": [],
            "inventory_mismatches": [],
            "orphaned_articles": [],
            "missing_review_metadata": [],
        }

    @staticmethod
    def token(html):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token was not rendered")
        return match.group(1).decode()

    def preview_url(self):
        session_id = self.fix_session["session_id"]
        return (
            f"/curator/tasks/{self.task_id}/structural-repair-preview"
            f"?curator_session={session_id}&origin=maintenance"
            f"&return_to=/curator/fix/{session_id}%3Fitem%3D{self.item_id}"
        )

    def approve(self):
        preview = self.client.get(self.preview_url())
        session_id = self.fix_session["session_id"]
        return self.client.post(
            f"/curator/tasks/{self.task_id}/structural-repair-approve",
            data={
                "csrf_token": self.token(preview.data),
                "approved": "yes",
                "curator_session": session_id,
                "origin": "maintenance",
                "return_to": f"/curator/fix/{session_id}?item={self.item_id}",
                "plan": '{"path":"/estimated_steps"}',
            },
        )

    def approval_id(self):
        directory = self.root / "curation_memory" / "structural_repair_approvals"
        return next(directory.iterdir()).name

    def apply(self, approval_id):
        session_id = self.fix_session["session_id"]
        confirmation = self.client.get(
            f"/curator/structural-repairs/{approval_id}"
            f"?curator_session={session_id}&origin=maintenance"
            f"&return_to=/curator/fix/{session_id}%3Fitem%3D{self.item_id}"
        )
        return self.client.post(
            f"/curator/structural-repairs/{approval_id}/apply",
            data={
                "csrf_token": self.token(confirmation.data),
                "curator_session": session_id,
                "origin": "maintenance",
                "return_to": f"/curator/fix/{session_id}?item={self.item_id}",
                "workflow": "malicious",
            },
        )

    def test_metadata_preview_and_server_bound_approval_are_read_only(self):
        memory_before = (self.root / "curation_memory" / "memory.json").read_bytes()
        preview = self.client.get(self.preview_url())

        self.assertEqual(preview.status_code, 200)
        for expected in (
            b"Review Progress Metadata Repair",
            b"Current metadata",
            b"Proposed metadata",
            b"progress_mode",
            b"absent",
            b"branch_aware",
            b"estimated_steps",
            b"Workflow graph",
            b"Publication",
            b"Remains Open",
        ):
            self.assertIn(expected, preview.data)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual((self.root / "curation_memory" / "memory.json").read_bytes(), memory_before)

        response = self.approve()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(
            (self.root / "curation_memory" / "structural_repair_applications").exists()
        )
        approval_id = self.approval_id()
        stored = json.loads(
            (self.root / "curation_memory" / "structural_repair_approvals"
             / approval_id / "approval.json").read_text(encoding="utf-8")
        )["approval"]
        self.assertEqual(stored["task_id"], self.task_id)
        self.assertEqual(stored["finding_id"], "CUR-PROGRESS-BROWSER")
        self.assertEqual(stored["reviewer_identity"], "Stage 3.7.1 Reviewer")
        self.assertEqual(stored["fix_session_id"], self.fix_session["session_id"])
        self.assertEqual(stored["workflow_id"], "higher_layer_connectivity")
        for field in (
            "workflow_raw_sha256_before",
            "workflow_semantic_sha256_before",
            "plan_digest",
            "specification_digest",
            "preview_digest",
        ):
            self.assertRegex(stored[field], r"^[0-9a-f]{64}$")
        confirmation = self.client.get(response.headers["Location"])
        self.assertIn(b"progress_mode", confirmation.data)
        self.assertIn(b"absent", confirmation.data)
        self.assertIn(b"branch_aware", confirmation.data)
        self.assertIn(b"Apply Approved Repair", confirmation.data)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_explicit_apply_result_and_exact_byte_restore_preserve_governance(self):
        self.approve()
        approval_id = self.approval_id()

        response = self.apply(approval_id)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Repair applied successfully to the editable draft", response.data)
        self.assertIn(b"progress_mode", response.data)
        self.assertIn(b"absent", response.data)
        self.assertIn(b"branch_aware", response.data)
        self.assertIn(b"estimated_steps", response.data)
        self.assertIn(b"Workflow graph", response.data)
        self.assertNotIn(b"Nodes added", response.data)
        applied = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(applied["progress_mode"], "branch_aware")
        self.assertEqual(applied["estimated_steps"], 4)
        self.assertEqual(applied["nodes"], self.workflow()["nodes"])
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")
        self.assertEqual(self.publication_state(), self.publication_before)

        application_root = self.root / "curation_memory" / "structural_repair_applications"
        application_id = next(application_root.iterdir()).name
        task_page = self.client.get(
            f"/curator/tasks/{self.task_id}?curator_session={self.fix_session['session_id']}"
            f"&origin=maintenance&return_to=/curator/fix/{self.fix_session['session_id']}"
        )
        self.assertIn(b"Progress metadata repair applied", task_page.data)
        self.assertIn(b"workflow graph was unchanged", task_page.data)
        self.assertNotIn(b"Progress metadata repair preview available", task_page.data)

        restore = self.client.get(
            f"/curator/structural-repairs/{application_id}/restore"
            f"?curator_session={self.fix_session['session_id']}&origin=maintenance"
            f"&return_to=/curator/fix/{self.fix_session['session_id']}"
        )
        restored = self.client.post(
            f"/curator/structural-repairs/{application_id}/restore",
            data={
                "csrf_token": self.token(restore.data),
                "curator_session": self.fix_session["session_id"],
                "origin": "maintenance",
                "return_to": f"/curator/fix/{self.fix_session['session_id']}",
                "confirmed": "yes",
                "reason": "Fixture acceptance requires exact-byte restoration.",
            },
        )
        self.assertEqual(restored.status_code, 200)
        self.assertIn(b"editable draft was restored exactly", restored.data)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")
        self.assertEqual(self.publication_state(), self.publication_before)


if __name__ == "__main__":
    unittest.main()
