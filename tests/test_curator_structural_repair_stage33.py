import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.app import app as flask_app
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_structural_repair_apply_service import StructuralRepairApplyError
from curator.memory import CuratorMemoryStore
from tests.structural_repair_fixtures import pre_stage34_network_diagnostics_bytes


class StructuralRepairStage33BrowserTests(unittest.TestCase):
    task_id = "GKT-STAGE33"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.drafts = self.root / "app" / "workflow_drafts"
        self.drafts.mkdir(parents=True)
        self.filename = "network_diagnostics.json"
        (self.drafts / self.filename).write_bytes(pre_stage34_network_diagnostics_bytes())
        self.before = (self.drafts / self.filename).read_bytes()
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        state = self.store.load()
        state["tasks"][self.task_id] = self.task()
        self.store.save(state)
        self.sessions = CuratorFixSessionService(self.root)
        self.fix_session = self.sessions.create(
            started_by="Stage 3.3 Reviewer", originating_audit_id=None,
            queue=[self.queue_item("FIX-STAGE33")], baseline={"counts": {}},
        )
        self.previous_root = flask_app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        flask_app.config.update(TESTING=True, STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root))
        self.client = flask_app.test_client()

    def tearDown(self):
        if self.previous_root is None:
            flask_app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            flask_app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = self.previous_root
        self.temporary.cleanup()

    @classmethod
    def task(cls):
        return {
            "task_id": cls.task_id, "finding_id": "CUR-STAGE33", "status": "open",
            "owner": "Curator", "priority": "Medium", "classification": "Recommendation",
            "confidence": "high", "knowledge_debt_score": 5, "times_observed": 1,
            "first_seen": "2026-08-25T00:00:00+00:00",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "title": "Terminal diagnosis may exceed collected evidence",
            "explanation": "The terminal requires evidence not collected on this path.",
            "recommended_action": "Review the governed structural repair.",
            "durable_identity": "terminal-evidence|network_diagnostics:dns_problem",
            "curator_rule": "CUR-WR-TERMINAL-EVIDENCE",
            "finding_type": "workflow_reasoning_evidence_gap",
            "content_type": "workflow_node",
            "content_identifier": "network_diagnostics:dns_problem",
            "related_workflows": ["network_diagnostics"], "history": [], "evidence": [],
            "structured_evidence": {
                "requirement": "dns_resolution", "terminal": "dns_problem",
                "missing": ["external_ip_reachability"], "affected_path_count": 1,
                "affected_paths": [{
                    "nodes": ["inspect_ip_configuration", "check_ip_address", "test_gateway",
                              "gateway_result", "test_dns", "dns_result", "dns_problem"],
                    "missing": ["external_ip_reachability"],
                    "predecessor_edge": {
                        "source": "dns_result", "route": "No", "destination": "dns_problem",
                    },
                }],
                "predecessor_edges": [{
                    "source": "dns_result", "route": "No", "destination": "dns_problem",
                }],
            },
        }

    @classmethod
    def queue_item(cls, item_id):
        return {
            "item_id": item_id, "status": "open",
            "classification": "STRUCTURAL_REVIEW_REQUIRED",
            "finding_type": "workflow_reasoning_evidence_gap",
            "knowledge_debt": 5,
            "affected_content": {"task_id": cls.task_id},
        }

    def preview_url(self, session_id=None):
        session_id = session_id or self.fix_session["session_id"]
        return (f"/curator/tasks/{self.task_id}/structural-repair-preview"
                f"?curator_session={session_id}&origin=maintenance"
                f"&return_to=/curator/fix/{session_id}%3Fitem%3DFIX-STAGE33")

    @staticmethod
    def token(html):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token was not rendered")
        return match.group(1).decode()

    def approve(self, **extra):
        preview = self.client.get(self.preview_url())
        data = {
            "csrf_token": self.token(preview.data), "approved": "yes",
            "curator_session": self.fix_session["session_id"], "origin": "maintenance",
            "return_to": f"/curator/fix/{self.fix_session['session_id']}?item=FIX-STAGE33",
        }
        data.update(extra)
        response = self.client.post(
            f"/curator/tasks/{self.task_id}/structural-repair-approve", data=data
        )
        return response

    def approval_id(self):
        directory = self.root / "curation_memory" / "structural_repair_approvals"
        return next(directory.iterdir()).name

    def apply(self, approval_id=None, **extra):
        approval_id = approval_id or self.approval_id()
        confirmation = self.client.get(
            f"/curator/structural-repairs/{approval_id}"
            f"?curator_session={self.fix_session['session_id']}&origin=maintenance"
            f"&return_to=/curator/fix/{self.fix_session['session_id']}%3Fitem%3DFIX-STAGE33"
        )
        data = {
            "csrf_token": self.token(confirmation.data),
            "curator_session": self.fix_session["session_id"], "origin": "maintenance",
            "return_to": f"/curator/fix/{self.fix_session['session_id']}?item=FIX-STAGE33",
        }
        data.update(extra)
        return self.client.post(f"/curator/structural-repairs/{approval_id}/apply", data=data)

    def test_task_entry_and_exact_preview_are_read_only(self):
        task_page = self.client.get(
            f"/curator/tasks/{self.task_id}?curator_session={self.fix_session['session_id']}"
            f"&origin=maintenance&return_to=/curator/fix/{self.fix_session['session_id']}"
        )
        self.assertEqual(task_page.status_code, 200)
        self.assertIn(b"Review Repair Preview", task_page.data)
        before_memory = (self.root / "curation_memory" / "memory.json").read_bytes()
        before_session = (self.root / "curation_memory" / "fix_sessions"
                          / f"{self.fix_session['session_id']}.json").read_bytes()

        preview = self.client.get(self.preview_url())

        self.assertEqual(preview.status_code, 200)
        for expected in (
            b"dns_result / No \xe2\x86\x92 dns_problem",
            b"dns_result / No \xe2\x86\x92 test_external_ip_reachability",
            b"test_external_ip_reachability \xe2\x86\x92 external_ip_reachability_result",
            b"replies_received \xe2\x86\x92 dns_problem",
            b"not_established \xe2\x86\x92 external_connectivity_unclear",
            b"I reviewed this repair preview and approve this exact proposed change.",
        ):
            self.assertIn(expected, preview.data)
        self.assertNotIn(b"Apply Approved Repair", preview.data)
        for forbidden_name in (b'name="plan"', b'name="workflow"', b'name="adapter_id"',
                               b'name="specification"', b'name="preview_digest"'):
            self.assertNotIn(forbidden_name, preview.data)
        self.assertEqual((self.drafts / self.filename).read_bytes(), self.before)
        self.assertEqual((self.root / "curation_memory" / "memory.json").read_bytes(), before_memory)
        self.assertEqual((self.root / "curation_memory" / "fix_sessions"
                          / f"{self.fix_session['session_id']}.json").read_bytes(), before_session)
        self.assertFalse((self.root / "curation_memory" / "structural_repair_approvals").exists())
        self.assertFalse((self.root / "curation_memory" / "structural_repair_applications").exists())

    def test_ineligible_task_has_no_supervised_entry(self):
        state = self.store.load()
        state["tasks"][self.task_id]["structured_evidence"]["missing"] = ["unknown_evidence"]
        self.store.save(state)
        response = self.client.get(self.preview_url())
        self.assertEqual(response.status_code, 404)
        task_page = self.client.get(
            f"/curator/tasks/{self.task_id}?curator_session={self.fix_session['session_id']}"
            f"&origin=maintenance&return_to=/curator/fix/{self.fix_session['session_id']}"
        )
        self.assertNotIn(b"Review Repair Preview", task_page.data)

    def test_approval_is_separate_csrf_protected_and_accepts_no_graph_authority(self):
        self.assertEqual(self.client.post(
            f"/curator/tasks/{self.task_id}/structural-repair-approve",
            data={"approved": "yes", "curator_session": self.fix_session["session_id"]},
        ).status_code, 400)
        response = self.approve(
            plan='{"malicious":true}', adapter_id="malicious", specification="malicious",
            workflow='{"nodes":{}}', preview_digest="0" * 64,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.drafts / self.filename).read_bytes(), self.before)
        self.assertFalse((self.root / "curation_memory" / "structural_repair_applications").exists())
        approval_id = self.approval_id()
        confirmation = self.client.get(response.headers["Location"])
        self.assertIn(b"Apply Approved Repair", confirmation.data)
        for forbidden_name in (b'name="plan"', b'name="workflow"', b'name="adapter_id"',
                               b'name="specification"', b'name="preview_digest"'):
            self.assertNotIn(forbidden_name, confirmation.data)
        stored = json.loads((self.root / "curation_memory" / "structural_repair_approvals"
                             / approval_id / "approval.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["approval"]["adapter_id"], "missing_required_upstream_evidence")
        created = datetime.fromisoformat(stored["approval"]["created_at"])
        expires = datetime.fromisoformat(stored["approval"]["expires_at"])
        self.assertEqual((expires - created).total_seconds(), 15 * 60)

    def test_stale_reviewed_preview_cannot_issue_approval(self):
        preview = self.client.get(self.preview_url())
        workflow = json.loads((self.drafts / self.filename).read_text(encoding="utf-8"))
        workflow["name"] = "Changed after reviewer opened preview"
        (self.drafts / self.filename).write_text(
            json.dumps(workflow, indent=4) + "\n", encoding="utf-8"
        )

        response = self.client.post(
            f"/curator/tasks/{self.task_id}/structural-repair-approve",
            data={
                "csrf_token": self.token(preview.data), "approved": "yes",
                "curator_session": self.fix_session["session_id"],
                "origin": "maintenance",
                "return_to": f"/curator/fix/{self.fix_session['session_id']}",
            },
        )

        self.assertEqual(response.status_code, 409)
        approvals = self.root / "curation_memory" / "structural_repair_approvals"
        self.assertFalse(approvals.exists())
        self.assertIn(b"Generate and review a new preview", response.data)

    def test_apply_is_separate_csrf_protected_writes_once_and_leaves_task_open(self):
        self.approve()
        approval_id = self.approval_id()
        self.assertEqual(self.client.post(
            f"/curator/structural-repairs/{approval_id}/apply",
            data={"curator_session": self.fix_session["session_id"]},
        ).status_code, 400)

        response = self.apply(approval_id, plan="malicious", workflow="malicious")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Repair applied successfully to the editable draft", response.data)
        self.assertIn(
            b"dns_result / No: dns_problem \xe2\x86\x92 test_external_ip_reachability",
            response.data,
        )
        self.assertNotIn(b"dns_result / No \xe2\x86\x92 dns_problem", response.data)
        workflow = json.loads((self.drafts / self.filename).read_text(encoding="utf-8"))
        self.assertIn("test_external_ip_reachability", workflow["nodes"])
        self.assertIn("external_ip_reachability_result", workflow["nodes"])
        self.assertIn("external_connectivity_unclear", workflow["nodes"])
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")
        self.assertFalse((self.root / "workflow_publications").exists())
        history = next((self.root / "curation_memory" / "structural_repair_applications").iterdir())
        self.assertEqual(len(list(history.glob("*.json"))), 2)

        task_page = self.client.get(
            f"/curator/tasks/{self.task_id}?curator_session={self.fix_session['session_id']}"
            f"&origin=maintenance&return_to=/curator/fix/{self.fix_session['session_id']}"
        )
        self.assertEqual(task_page.status_code, 200)
        self.assertIn(b"Structural repair applied", task_page.data)
        self.assertIn(b"editable workflow draft was updated", task_page.data)
        self.assertIn(b"Verify Current Content", task_page.data)
        self.assertIn(b"workflow was not published", task_page.data)
        self.assertNotIn(b"Structural repair preview available", task_page.data)
        self.assertNotIn(b"Review Repair Preview", task_page.data)
        self.assertIn(b"Restore Pre-Repair Draft", task_page.data)
        self.assertEqual(self.store.load()["tasks"][self.task_id]["status"], "open")

        replay = self.apply(approval_id)
        self.assertEqual(replay.status_code, 200)
        self.assertIn(b"Repair was already applied", replay.data)
        self.assertEqual(len(list(history.glob("*.json"))), 2)

    def test_wrong_session_and_stale_workflow_fail_closed(self):
        self.approve()
        approval_id = self.approval_id()
        other = self.sessions.create(
            started_by="Other Reviewer", originating_audit_id=None,
            queue=[self.queue_item("FIX-OTHER")], baseline={"counts": {}},
        )
        with self.client.session_transaction() as browser:
            token = browser["structural_repair_csrf"]
        wrong = self.client.post(f"/curator/structural-repairs/{approval_id}/apply", data={
            "csrf_token": token, "curator_session": other["session_id"],
        })
        self.assertEqual(wrong.status_code, 422)
        self.assertEqual((self.drafts / self.filename).read_bytes(), self.before)

        (self.drafts / self.filename).write_bytes(self.before + b" ")
        stale = self.apply(approval_id)
        self.assertEqual(stale.status_code, 422)
        self.assertIn(b"editable workflow changed after approval", stale.data.lower())

    def test_bounded_failure_states_are_presented_without_internal_detail(self):
        self.approve()
        approval_id = self.approval_id()
        cases = {
            "approval_expired": b"approval expired",
            "preview_unknown": b"preview or evidence specification changed",
            "lock_unavailable": b"currently being edited",
            "rollback_succeeded": b"restored exactly",
            "rollback_failed": b"Manual intervention is required",
        }
        for code, expected in cases.items():
            with self.subTest(code=code), patch(
                "app.services.curator_structural_repair_apply_service."
                "CuratorStructuralRepairApplyService.apply",
                side_effect=StructuralRepairApplyError(code, "secret internal path"),
            ):
                response = self.apply(approval_id)
                self.assertIn(expected, response.data)
                self.assertNotIn(b"secret internal path", response.data)
                if code == "lock_unavailable":
                    self.assertIn(b"Retry Apply", response.data)


if __name__ == "__main__":
    unittest.main()
