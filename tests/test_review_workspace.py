import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")

from app.app import app
from app.services.curator_task_navigation_service import CuratorTaskNavigationService
from app.services.curator_workflow_lifecycle_service import CuratorWorkflowLifecycleService
from app.services.review_workspace_service import ReviewWorkspaceService
from curator.growth import CuratorGrowthService as GrowthStoreService
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from tests.test_accessibility import InteractiveParser


class ReviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CuratorMemoryStore(self.root / "curation_memory")
        self.previous_root = app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
        self.previous_testing = app.testing
        app.config.update(TESTING=True, AUTH_TEST_BYPASS=True,
                          STRUCTURAL_REPAIR_REPOSITORY_ROOT=str(self.root))
        self.client = app.test_client()

    def tearDown(self):
        if self.previous_root is None:
            app.config.pop("STRUCTURAL_REPAIR_REPOSITORY_ROOT", None)
        else:
            app.config["STRUCTURAL_REPAIR_REPOSITORY_ROOT"] = self.previous_root
        app.config["TESTING"] = self.previous_testing
        self.temporary.cleanup()

    @staticmethod
    def task(task_id, **overrides):
        value = {
            "task_id": task_id, "finding_id": f"CUR-{task_id}", "status": "open",
            "owner": "Human", "priority": "Medium", "classification": "Observation",
            "finding_type": "test_finding", "curator_rule": "CUR-TEST",
            "title": f"Review {task_id}", "content_type": "application",
            "content_identifier": "gnojo", "explanation": "A deterministic condition was found.",
            "recommended_action": "Review current evidence.", "confidence": "high",
            "knowledge_debt_score": 2, "times_observed": 2,
            "related_content": [], "related_workflows": [], "related_articles": [],
            "related_commands": [], "related_scripts": [], "evidence": ["Evidence"],
            "history": [], "resolution_history": [],
        }
        value.update(overrides)
        return value

    def save_tasks(self, *tasks):
        state = self.store.load()
        state["tasks"] = {task["task_id"]: task for task in tasks}
        self.store.save(state)

    def add_growth(self):
        growth = GrowthStoreService(self.store)
        lesson = growth.record_lesson({
            "pattern_observed": "review_clearer_evidence",
            "supporting_evidence": ["GKT-ONE"],
            "recommended_future_behavior": "Prefer clearer current evidence.",
            "confidence": "high", "observations": 3,
        })
        proposal = growth.propose("capability", {
            "proposed_capability": "Summarize repeated evidence",
            "problem_addressed": "Reviewers repeatedly scan the same evidence.",
            "supporting_task_ids": ["GKT-ONE"], "recurrence_count": 3,
            "expected_benefit": "Reduce review effort.", "scope": "Curator summaries",
            "required_tools": [], "required_permissions": [], "risks": ["Summary could omit context."],
            "test_plan": ["Compare output."], "rollback_plan": "Reject proposal.",
            "confidence": "high",
        })
        excluded = growth.propose("audit_rule", {
            "proposed_capability": "Detect another rule",
            "problem_addressed": "A staged rule needs testing.",
            "supporting_task_ids": [], "recurrence_count": 1,
            "expected_benefit": "More findings.", "scope": "Audit",
            "required_tools": [], "required_permissions": [], "risks": ["False positive findings."],
            "test_plan": ["Shadow test."], "rollback_plan": "Reject.", "confidence": "low",
        })
        return lesson, proposal, excluded

    def test_get_is_read_only_and_aggregates_only_supported_items(self):
        self.save_tasks(
            self.task("GKT-OPEN"),
            self.task("GKT-DEFERRED", status="deferred"),
            self.task("GKT-RESOLVED", status="resolved"),
        )
        lesson, proposal, excluded = self.add_growth()
        before = (self.root / "curation_memory/memory.json").read_bytes()
        response = self.client.get("/review")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        titles = {item["title"] for item in ReviewWorkspaceService(self.root).items()}
        self.assertIn("Review GKT-OPEN", titles)
        self.assertIn("Review Clearer Evidence", titles)
        self.assertIn("Summarize repeated evidence", titles)
        self.assertNotIn("Review GKT-RESOLVED", titles)
        self.assertNotIn(excluded["proposal_id"], json.dumps(list(titles)))
        self.assertNotIn("publication reasoning", page.casefold())
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)
        self.assertFalse((self.root / "curation_memory/resolution_packages").exists())

    def test_deterministic_ordering_uses_priority_groups_and_stable_ids(self):
        self.save_tasks(
            self.task("GKT-Z", priority="Low", confidence="high", times_observed=8),
            self.task("GKT-B", priority="High", classification="Risk"),
            self.task("GKT-A", priority="High", classification="Risk"),
            self.task("GKT-U", priority="Medium", confidence="low", times_observed=1),
            self.task("GKT-C", priority="Medium", confidence="high", times_observed=4,
                      current_verification={"status": "appears_corrected"}),
        )
        keys = [item["key"] for item in ReviewWorkspaceService(self.root).items()]
        self.assertEqual(keys[:2], ["curator_task:GKT-A", "curator_task:GKT-B"])
        self.assertLess(keys.index("curator_task:GKT-U"), keys.index("curator_task:GKT-C"))
        self.assertLess(keys.index("curator_task:GKT-C"), keys.index("curator_task:GKT-Z"))
        self.assertEqual(keys, [item["key"] for item in ReviewWorkspaceService(self.root).items()])

    def test_curator_compression_uses_deterministic_identity_not_titles(self):
        shared = {
            "curator_rule": "CUR-SAFE-L1", "finding_type": "missing_safety_guidance",
            "content_type": "workflow_node", "safety_level": 1, "category": "Safety",
        }
        self.save_tasks(
            self.task("GKT-A", title="Restart guidance missing", **shared),
            self.task("GKT-B", title="Protect work before closing", **shared),
            self.task("GKT-C", title="Restart guidance missing", **{
                **shared, "curator_rule": "CUR-SAFE-L2",
            }),
        )
        items = {item["item_id"]: item for item in ReviewWorkspaceService(self.root).items()}
        self.assertEqual(items["GKT-A"]["compression"]["similar_open_count"], 2)
        self.assertEqual(items["GKT-B"]["compression"]["similar_open_count"], 2)
        self.assertEqual(items["GKT-C"]["compression"]["similar_open_count"], 1)
        self.assertIn("safety level 1", items["GKT-A"]["compression"]["basis"])

    def test_reasoning_tasks_group_by_existing_structural_fingerprint(self):
        evidence = [
            "Branches converge.",
            ("Structural evidence: {'branch_labels': ['Yes', 'No'], "
             "'destinations': ['left', 'right'], 'convergence_node': 'join', "
             "'distance': 2}"),
        ]
        common = {
            "curator_rule": "CUR-WR-EARLY-CONVERGENCE",
            "finding_type": "workflow_reasoning_early_convergence",
            "content_type": "workflow_node", "evidence": evidence,
        }
        self.save_tasks(
            self.task("GKT-A", content_identifier="printer:power", **common),
            self.task("GKT-B", content_identifier="vpn:credentials", **common),
        )
        items = ReviewWorkspaceService(self.root).items()
        self.assertTrue(all(
            item["compression"]["similar_open_count"] == 2 for item in items
        ))
        self.assertIn(
            "same deterministic workflow structure", items[0]["compression"]["basis"]
        )

    def test_consistent_mixed_and_missing_precedent_classifications(self):
        shared = {
            "curator_rule": "CUR-SAFE-L1", "finding_type": "missing_safety_guidance",
            "content_type": "workflow_node", "safety_level": 1,
        }
        self.save_tasks(
            self.task("GKT-CURRENT", **shared),
            self.task("GKT-RES-1", status="resolved", **shared),
            self.task("GKT-RES-2", status="resolved", **shared),
        )
        compression = ReviewWorkspaceService(self.root).items()[0]["compression"]
        self.assertEqual(compression["classification"], "Routine pattern")
        self.assertEqual(compression["prior_dispositions"], {"Resolved": 2})

        self.save_tasks(
            self.task("GKT-CURRENT", **shared),
            self.task("GKT-RES", status="resolved", **shared),
            self.task("GKT-IGNORE", status="ignored", **shared),
        )
        compression = ReviewWorkspaceService(self.root).items()[0]["compression"]
        self.assertEqual(compression["classification"], "Mixed precedent")
        self.assertEqual(compression["prior_count"], 2)

        self.save_tasks(self.task("GKT-CURRENT", **shared))
        compression = ReviewWorkspaceService(self.root).items()[0]["compression"]
        self.assertEqual(
            compression["classification"], "Novel / insufficient precedent"
        )
        self.assertEqual(compression["prior_count"], 0)

    def test_growth_reasoning_lessons_group_by_rule_and_calibration_pattern(self):
        self.save_tasks()
        growth = GrowthStoreService(self.store)
        for disposition in ("useful", "intentional"):
            growth.record_lesson({
                "pattern_observed": (
                    "reasoning_calibration:cur-wr-early-convergence:"
                    f"rcp-shared:{disposition}"
                ),
                "supporting_evidence": [f"GKT-{disposition}"],
                "recommended_future_behavior": "Keep the pattern under review.",
            })
        items = ReviewWorkspaceService(self.root).items()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(
            item["compression"]["similar_open_count"] == 2 for item in items
        ))
        self.assertTrue(all(
            "Early Branch Convergence" in item["compression"]["basis"]
            for item in items
        ))

    def test_growth_precedent_uses_authoritative_lesson_decisions(self):
        self.save_tasks()
        growth = GrowthStoreService(self.store)
        lessons = []
        for disposition in ("useful", "intentional", "false_positive"):
            lessons.append(growth.record_lesson({
                "pattern_observed": (
                    "reasoning_calibration:cur-wr-early-convergence:"
                    f"rcp-shared:{disposition}"
                ),
                "supporting_evidence": [f"GKT-{disposition}"],
                "recommended_future_behavior": "Keep the pattern under review.",
            }))
        for lesson in lessons[:2]:
            growth.decide_lesson(
                lesson["lesson_id"], "approved",
                reviewer="Greg", reason="Reviewed precedent.",
            )
        current = ReviewWorkspaceService(self.root).items()[0]["compression"]
        self.assertEqual(current["classification"], "Routine pattern")
        self.assertEqual(current["prior_dispositions"], {"Approved": 2})

        growth.decide_lesson(
            lessons[1]["lesson_id"], "rejected",
            reviewer="Greg", reason="Conflicting evidence.",
        )
        current = ReviewWorkspaceService(self.root).items()[0]["compression"]
        self.assertEqual(current["classification"], "Mixed precedent")
        self.assertEqual(current["prior_dispositions"], {"Approved": 1, "Rejected": 1})

    def test_compression_is_stable_read_only_and_adds_no_bulk_controls(self):
        self.save_tasks(self.task("GKT-A"), self.task("GKT-B"))
        before = (self.root / "curation_memory/memory.json").read_bytes()
        first = ReviewWorkspaceService(self.root).workspace()
        second = ReviewWorkspaceService(self.root).workspace()
        self.assertEqual(
            [item["compression"] for item in first["items"]],
            [item["compression"] for item in second["items"]],
        )
        page = self.client.get("/review").get_data(as_text=True)
        self.assertIn("Review compression", page)
        self.assertIn("Novel / insufficient precedent", page)
        for forbidden in ("Accept similar items", "Resolve all", "Ignore all"):
            self.assertNotIn(forbidden, page)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_curator_actions_delegate_and_advance_without_parallel_state(self):
        for decision, expected in (("resolve", "resolved"), ("defer", "deferred"),
                                   ("ignore", "ignored")):
            with self.subTest(decision=decision):
                self.save_tasks(self.task("GKT-A"), self.task("GKT-B"))
                service = ReviewWorkspaceService(self.root)
                item = service.find("curator_task", "GKT-A")
                response = self.client.post(
                    "/review/curator_task/GKT-A/decision",
                    data={"source_fingerprint": item["source_fingerprint"],
                          "decision": decision, "reason": "Reviewed current evidence."},
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn("notice=saved", response.location)
                self.assertIn("curator_task:GKT-B", response.location)
                state = self.store.load()
                self.assertEqual(state["tasks"]["GKT-A"]["status"], expected)
                self.assertNotIn("review", state)

    def test_growth_accept_reject_and_skip_use_existing_growth_state(self):
        self.save_tasks()
        lesson, proposal, _ = self.add_growth()
        service = ReviewWorkspaceService(self.root)
        lesson_item = service.find("growth_lesson", lesson["lesson_id"])
        self.client.post(
            f"/review/growth_lesson/{lesson['lesson_id']}/decision",
            data={"source_fingerprint": lesson_item["source_fingerprint"],
                  "decision": "approve", "reason": "Evidence supports this lesson."},
        )
        proposal_item = ReviewWorkspaceService(self.root).find(
            "growth_capability", proposal["proposal_id"]
        )
        self.client.post(
            f"/review/growth_capability/{proposal['proposal_id']}/decision",
            data={"source_fingerprint": proposal_item["source_fingerprint"],
                  "decision": "reject", "reason": "The benefit is not sufficient."},
        )
        state = self.store.load()
        self.assertEqual(state["growth"]["lessons"][lesson["lesson_id"]]["status"], "approved")
        self.assertEqual(state["growth"]["proposals"][proposal["proposal_id"]]["status"], "rejected")

        # Skip is a plain GET to the next projected item and creates no decision.
        self.save_tasks(self.task("GKT-A"), self.task("GKT-B"))
        before = (self.root / "curation_memory/memory.json").read_bytes()
        self.client.get("/review?item=curator_task:GKT-B")
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_concurrent_item_change_fails_closed(self):
        self.save_tasks(self.task("GKT-A"))
        item = ReviewWorkspaceService(self.root).find("curator_task", "GKT-A")
        state = self.store.load()
        state["tasks"]["GKT-A"]["priority"] = "High"
        self.store.save(state)
        response = self.client.post(
            "/review/curator_task/GKT-A/decision",
            data={"source_fingerprint": item["source_fingerprint"],
                  "decision": "ignore", "reason": "Stale decision."},
        )
        self.assertIn("notice=changed", response.location)
        self.assertEqual(self.store.load()["tasks"]["GKT-A"]["status"], "open")

    def test_fresh_corrected_shortcut_requires_exact_current_fingerprint(self):
        workflow = {"workflow_id": "sample", "name": "Sample", "nodes": {
            "start": {"type": "resolution", "message": "Done"}
        }, "start_node": "start"}
        path = self.root / "app/decision_trees/sample.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(workflow), encoding="utf-8")
        fingerprint = CuratorWorkflowLifecycleService.fingerprint(workflow)
        task = self.task(
            "GKT-FRESH", content_type="workflow", content_identifier="sample",
            last_verified_fingerprint=fingerprint,
            current_verification={
                "status": "appears_corrected", "workflow_id": "sample",
                "affected_fingerprint": fingerprint,
                "affected_fingerprint_scope": "whole_workflow",
            },
        )
        self.save_tasks(task)
        service = ReviewWorkspaceService(self.root)
        self.assertTrue(service.find("curator_task", "GKT-FRESH")["resolve_verified"])
        state = self.store.load()
        state["tasks"]["GKT-FRESH"]["current_verification"]["affected_fingerprint"] = "stale"
        self.store.save(state)
        self.assertFalse(
            ReviewWorkspaceService(self.root).find("curator_task", "GKT-FRESH")["resolve_verified"]
        )
        state = self.store.load()
        state["tasks"]["GKT-FRESH"]["current_verification"] = {}
        self.store.save(state)
        self.assertFalse(
            ReviewWorkspaceService(self.root).find("curator_task", "GKT-FRESH")["resolve_verified"]
        )

    def test_review_origin_is_bounded_and_round_trips_same_item(self):
        valid = "/review?item=curator_task:GKT-ONE"
        navigation = CuratorTaskNavigationService.resolve(
            "review_workspace", valid, task_id="GKT-ONE"
        )
        self.assertEqual(navigation.origin, "review_workspace")
        self.assertEqual(navigation.return_label, "Return to Review")
        self.assertEqual(navigation.return_url, valid)
        for unsafe in (
            "https://evil.example/review", "//evil.example/review",
            "/review?item=curator_task:GKT-OTHER", "/review?item=x&unexpected=1",
        ):
            with self.subTest(unsafe=unsafe):
                result = CuratorTaskNavigationService.resolve(
                    "review_workspace", unsafe, task_id="GKT-ONE"
                )
                self.assertEqual(result.origin, "overview")

    def test_review_requires_auth_and_navigation_is_role_aware(self):
        self.save_tasks(self.task("GKT-A"))
        app.config.update(
            TESTING=False, AUTH_TEST_BYPASS=False,
            GNOJO_STABLE_SESSION_SECRET_CONFIGURED=True,
            GNOJO_REVIEWER_USERNAME="reviewer",
            GNOJO_REVIEWER_PASSWORD_HASH="not-a-valid-hash",
        )
        anonymous = app.test_client()
        self.assertEqual(anonymous.get("/review").status_code, 302)
        before = (self.root / "curation_memory/memory.json").read_bytes()
        self.assertEqual(
            anonymous.post(
                "/review/curator_task/GKT-A/decision",
                data={"source_fingerprint": "anything", "decision": "ignore"},
            ).status_code,
            403,
        )
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)
        public = anonymous.get("/").get_data(as_text=True)
        self.assertNotIn('href="/review"', public)
        app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)
        authenticated = app.test_client().get("/review").get_data(as_text=True)
        self.assertIn('href="/review"', authenticated)

    def test_review_page_offers_explicit_verify_without_running_it_on_get(self):
        self.save_tasks(self.task("GKT-A"))
        before = (self.root / "curation_memory/memory.json").read_bytes()
        with patch("app.app.CuratorTargetedVerificationService.verify") as verify:
            page = self.client.get("/review").get_data(as_text=True)
        verify.assert_not_called()
        self.assertIn("Verify Current Content", page)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_review_page_controls_are_named_and_use_one_main_landmark(self):
        self.save_tasks(self.task("GKT-A"))
        page = self.client.get("/review").get_data(as_text=True)
        parser = InteractiveParser()
        parser.feed(page)
        self.assertEqual(parser.main_count, 1)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertIn("reviewReason", parser.labels_for)
        for button in parser.buttons:
            self.assertTrue(button["text"].strip() or button["attrs"].get("aria-label"))

    def test_task_detail_returns_to_the_exact_review_item(self):
        self.save_tasks(self.task("GKT-A"))
        page = self.client.get(
            "/curator/tasks/GKT-A?origin=review_workspace"
            "&return_to=/review%3Fitem%3Dcurator_task%253AGKT-A"
        ).get_data(as_text=True)
        self.assertIn("Return to Review", page)
        self.assertIn('href="/review?item=curator_task%3AGKT-A"', page)

    @staticmethod
    def routine_fields():
        return {
            "curator_rule": "CUR-SAFE-L1", "finding_type": "missing_safety_guidance",
            "content_type": "workflow_node", "content_identifier": "sample:step",
            "safety_level": 1, "category": "Safety",
        }

    def save_routine_curator_group(self):
        shared = self.routine_fields()
        self.save_tasks(
            self.task("GKT-A", content_identifier="sample:a", **{
                key: value for key, value in shared.items() if key != "content_identifier"
            }),
            self.task("GKT-B", content_identifier="sample:b", **{
                key: value for key, value in shared.items() if key != "content_identifier"
            }),
            self.task("GKT-OLD-1", status="resolved", **shared),
            self.task("GKT-OLD-2", status="resolved", **shared),
        )

    def test_routine_curator_group_preview_is_exact_and_read_only(self):
        self.save_routine_curator_group()
        before = (self.root / "curation_memory/memory.json").read_bytes()
        page = self.client.get("/review?item=curator_task:GKT-A").get_data(as_text=True)
        self.assertIn("Review similar items (2)", page)
        response = self.client.get("/review/curator_task/GKT-A/batch")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        for value in ("GKT-A", "GKT-B", "sample:a", "sample:b", "No decision has been applied yet"):
            self.assertIn(value, page)
        self.assertNotIn("GKT-OLD-1</strong>", page)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

        parser = InteractiveParser()
        parser.feed(page)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for field in ("batchDecision", "batchReason", "batchConfirmed"):
            self.assertIn(field, parser.labels_for)
        for button in parser.buttons:
            self.assertTrue(button["text"].strip() or button["attrs"].get("aria-label"))

    def test_mixed_novel_and_capability_items_do_not_offer_batching(self):
        shared = self.routine_fields()
        self.save_tasks(
            self.task("GKT-A", **shared),
            self.task("GKT-RES", status="resolved", **shared),
            self.task("GKT-IGNORE", status="ignored", **shared),
        )
        page = self.client.get("/review?item=curator_task:GKT-A").get_data(as_text=True)
        self.assertNotIn("Review similar items", page)
        response = self.client.get("/review/curator_task/GKT-A/batch")
        self.assertEqual(response.status_code, 302)

        self.save_tasks(self.task("GKT-A", **shared), self.task("GKT-B", **shared))
        self.assertNotIn(
            "Review similar items",
            self.client.get("/review?item=curator_task:GKT-A").get_data(as_text=True),
        )

        self.save_tasks()
        _, proposal, _ = self.add_growth()
        item = ReviewWorkspaceService(self.root).find("growth_capability", proposal["proposal_id"])
        self.assertFalse(item["batch_review_available"])

    def test_structurally_different_reasoning_item_is_excluded(self):
        common = {
            "curator_rule": "CUR-WR-EARLY-CONVERGENCE",
            "finding_type": "workflow_reasoning_early_convergence",
            "content_type": "workflow_node",
        }
        evidence_a = ["Structural evidence: {'branches': ['yes', 'no'], 'distance': 2}"]
        evidence_b = ["Structural evidence: {'branches': ['yes', 'no'], 'distance': 4}"]
        self.save_tasks(
            self.task("GKT-A", content_identifier="one:a", evidence=evidence_a, **common),
            self.task("GKT-B", content_identifier="two:b", evidence=evidence_a, **common),
            self.task("GKT-C", content_identifier="three:c", evidence=evidence_b, **common),
            self.task("GKT-OLD-1", status="resolved", content_identifier="old:a", evidence=evidence_a, **common),
            self.task("GKT-OLD-2", status="resolved", content_identifier="old:b", evidence=evidence_a, **common),
        )
        items = {item["item_id"]: item for item in ReviewWorkspaceService(self.root).items()}
        self.assertEqual(items["GKT-A"]["batch_review_count"], 2)
        self.assertFalse(items["GKT-C"]["batch_review_available"])

    def test_batch_requires_confirmation_reason_and_current_snapshot(self):
        self.save_routine_curator_group()
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        before = (self.root / "curation_memory/memory.json").read_bytes()
        missing_confirmation = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "Reviewed."},
        )
        self.assertIn("notice=invalid", missing_confirmation.location)
        missing_reason = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "", "confirmed": "yes"},
        )
        self.assertIn("notice=invalid", missing_reason.location)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

        state = self.store.load()
        state["tasks"]["GKT-B"]["priority"] = "High"
        self.store.save(state)
        changed = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "Reviewed.", "confirmed": "yes"},
        )
        self.assertIn("notice=changed", changed.location)
        state = self.store.load()
        self.assertEqual(state["tasks"]["GKT-A"]["status"], "open")
        self.assertEqual(state["tasks"]["GKT-B"]["status"], "open")

    def test_changed_authoritative_content_fingerprint_fails_closed(self):
        workflow = {"workflow_id": "sample", "name": "Sample", "start_node": "a", "nodes": {
            "a": {"type": "resolution", "message": "Before"}
        }}
        path = self.root / "app/decision_trees/sample.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workflow), encoding="utf-8")
        shared = self.routine_fields()
        self.save_tasks(
            self.task("GKT-A", content_identifier="sample:a", **{
                key: value for key, value in shared.items() if key != "content_identifier"
            }),
            self.task("GKT-B", content_identifier="sample:a", **{
                key: value for key, value in shared.items() if key != "content_identifier"
            }),
            self.task("GKT-OLD-1", status="resolved", **shared),
            self.task("GKT-OLD-2", status="resolved", **shared),
        )
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        workflow["nodes"]["a"]["message"] = "After"
        path.write_text(json.dumps(workflow), encoding="utf-8")
        before = (self.root / "curation_memory/memory.json").read_bytes()
        response = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "Reviewed.", "confirmed": "yes"},
        )
        self.assertIn("notice=changed", response.location)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_independently_resolved_candidate_fails_closed(self):
        self.save_routine_curator_group()
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.store.update_task("GKT-B", status="resolved", note="Independent review.", event_name="resolve")
        response = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "Group review.", "confirmed": "yes"},
        )
        self.assertIn("notice=changed", response.location)
        self.assertEqual(self.store.load()["tasks"]["GKT-A"]["status"], "open")

    def test_curator_batch_is_atomic_and_preserves_per_item_history(self):
        self.save_routine_curator_group()
        state = self.store.load()
        state["tasks"]["GKT-NEXT"] = self.task(
            "GKT-NEXT", curator_rule="CUR-OTHER", finding_type="other_finding",
            content_identifier="other:step",
        )
        self.store.save(state)
        service = ReviewWorkspaceService(self.root)
        preview = service.batch_preview("curator_task", "GKT-A")
        response = self.client.post(
            "/review/curator_task/GKT-A/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "ignore", "reason": "Same reviewed safety pattern.",
                  "confirmed": "yes"},
        )
        self.assertIn("notice=batch_saved", response.location)
        self.assertIn("item=curator_task:GKT-NEXT", response.location)
        self.assertIn("count=2", response.location)
        state = self.store.load()
        for task_id in ("GKT-A", "GKT-B"):
            self.assertEqual(state["tasks"][task_id]["status"], "ignored")
            self.assertEqual(state["tasks"][task_id]["history"][-1]["note"],
                             "Same reviewed safety pattern.")
        self.assertEqual(sum(event.get("event") == "ignore" for event in state["decisions"]), 2)

        self.save_routine_curator_group()
        before = (self.root / "curation_memory/memory.json").read_bytes()
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        original = ReviewWorkspaceService._apply_existing_action
        calls = []
        def fail_second(root, item_type, item_id, decision, reason, reviewer):
            calls.append(item_id)
            if len(calls) == 2:
                raise CuratorMemoryError("Simulated second-item failure.")
            return original(root, item_type, item_id, decision, reason, reviewer)
        with patch.object(ReviewWorkspaceService, "_apply_existing_action", side_effect=fail_second):
            response = self.client.post(
                "/review/curator_task/GKT-A/batch/decision",
                data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                      "decision": "ignore", "reason": "Review.", "confirmed": "yes"},
            )
        self.assertIn("notice=invalid", response.location)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_resolve_verified_requires_every_candidate_to_be_fresh(self):
        self.save_routine_curator_group()
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.assertNotIn("resolve_verified", preview["actions"])
        self.assertIn("resolve", preview["actions"])

        state = self.store.load()
        state["tasks"]["GKT-B"]["status"] = "deferred"
        self.store.save(state)
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.assertNotIn("defer", preview["actions"])

    def test_resolve_verified_requires_fresh_state_for_every_item(self):
        workflows = {}
        for workflow_id in ("sample_a", "sample_b"):
            workflow = {"workflow_id": workflow_id, "name": workflow_id, "nodes": {
                "start": {"type": "resolution", "message": "Done"}
            }, "start_node": "start"}
            path = self.root / f"app/decision_trees/{workflow_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(workflow), encoding="utf-8")
            workflows[workflow_id] = CuratorWorkflowLifecycleService.fingerprint(workflow)
        shared = {
            "curator_rule": "CUR-TEST", "finding_type": "test_finding",
            "content_type": "workflow", "category": "Quality",
        }
        tasks = []
        for suffix, workflow_id in (("A", "sample_a"), ("B", "sample_b")):
            fingerprint = workflows[workflow_id]
            tasks.append(self.task(
                f"GKT-{suffix}", content_identifier=workflow_id,
                last_verified_fingerprint=fingerprint,
                current_verification={
                    "status": "appears_corrected", "workflow_id": workflow_id,
                    "affected_fingerprint": fingerprint,
                    "affected_fingerprint_scope": "whole_workflow",
                }, **shared,
            ))
        tasks.extend((self.task("GKT-OLD-1", status="resolved", **shared),
                      self.task("GKT-OLD-2", status="resolved", **shared)))
        self.save_tasks(*tasks)
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.assertIn("resolve_verified", preview["actions"])
        self.assertNotIn("resolve", preview["actions"])

        state = self.store.load()
        state["tasks"]["GKT-B"]["current_verification"]["affected_fingerprint"] = "stale"
        self.store.save(state)
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.assertNotIn("resolve_verified", preview["actions"])
        self.assertIn("resolve", preview["actions"])

    def test_preview_lists_ineligible_same_pattern_item_as_excluded(self):
        self.save_routine_curator_group()
        state = self.store.load()
        state["tasks"]["GKT-C"] = self.task(
            "GKT-C", content_identifier="sample:c", classification="Integrity",
            **{key: value for key, value in self.routine_fields().items()
               if key not in {"content_identifier"}},
        )
        self.store.save(state)
        preview = ReviewWorkspaceService(self.root).batch_preview("curator_task", "GKT-A")
        self.assertEqual([entry["item"]["item_id"] for entry in preview["excluded"]], ["GKT-C"])
        page = self.client.get("/review/curator_task/GKT-A/batch").get_data(as_text=True)
        self.assertIn("Excluded items", page)
        self.assertIn("specialized supervised review path", page)

    def test_review_workspace_explains_when_similar_items_are_not_batch_eligible(self):
        shared = self.routine_fields()
        common = {key: value for key, value in shared.items() if key != "content_identifier"}
        self.save_tasks(
            self.task("GKT-A", content_identifier="sample:a", **common),
            self.task(
                "GKT-B", content_identifier="sample:b", classification="Integrity", **common,
            ),
            self.task("GKT-C", finding_id="", content_identifier="sample:c", **common),
            self.task("GKT-OLD-1", status="resolved", **shared),
            self.task("GKT-OLD-2", status="resolved", **shared),
        )
        before = (self.root / "curation_memory/memory.json").read_bytes()
        service = ReviewWorkspaceService(self.root)
        items = {item["item_id"]: item for item in service.items()}
        current = items["GKT-A"]

        self.assertEqual(current["compression"]["classification"], "Routine pattern")
        self.assertEqual(current["compression"]["similar_open_count"], 3)
        self.assertEqual(current["batch_review_count"], 1)
        self.assertEqual(current["batch_excluded_count"], 2)
        expected_reasons = {
            service._batch_exclusion_reason(items["GKT-B"]),
            service._batch_exclusion_reason(items["GKT-C"]),
        }
        self.assertEqual(
            {entry["reason"] for entry in current["batch_exclusion_reasons"]},
            expected_reasons,
        )

        page = self.client.get("/review?item=curator_task:GKT-A").get_data(as_text=True)
        self.assertIn("3 similar open items. 1 currently qualifies", page)
        self.assertIn("2 similar items are excluded", page)
        self.assertIn("requires its specialized supervised review path", page)
        self.assertIn("does not have an unambiguous authoritative identity", page)
        self.assertNotIn("Review similar items (", page)
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)

    def test_all_eligible_similar_items_keep_batch_action_without_warning(self):
        self.save_routine_curator_group()
        page = self.client.get("/review?item=curator_task:GKT-A").get_data(as_text=True)
        self.assertIn("Review similar items (2)", page)
        self.assertNotIn("currently qualifies for governed batch review", page)
        self.assertNotIn("similar items are excluded", page)

    def test_growth_lesson_batch_delegates_and_has_no_fake_defer(self):
        self.save_tasks()
        growth = GrowthStoreService(self.store)
        lessons = [growth.record_lesson({
            "pattern_observed": f"reasoning_calibration:cur-wr-early-convergence:rcp-shared:{suffix}",
            "supporting_evidence": [suffix], "recommended_future_behavior": "Retain this pattern.",
        }) for suffix in ("one", "two", "old-one", "old-two")]
        for lesson in lessons[2:]:
            growth.decide_lesson(lesson["lesson_id"], "approved", reviewer="Greg", reason="Prior review.")
        service = ReviewWorkspaceService(self.root)
        preview = service.batch_preview("growth_lesson", lessons[0]["lesson_id"])
        self.assertEqual(preview["actions"], ("approve", "reject"))
        self.assertNotIn("defer", json.dumps(preview))
        response = self.client.post(
            f"/review/growth_lesson/{lessons[0]['lesson_id']}/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "approve", "reason": "Same governed lesson pattern.",
                  "confirmed": "yes"},
        )
        self.assertIn("notice=batch_saved", response.location)
        state = self.store.load()
        for lesson in lessons[:2]:
            current = state["growth"]["lessons"][lesson["lesson_id"]]
            self.assertEqual(current["status"], "approved")
            self.assertEqual(current["decision_history"][-1]["reason"],
                             "Same governed lesson pattern.")

        rejected = [growth.record_lesson({
            "pattern_observed": f"reasoning_calibration:cur-wr-early-convergence:rcp-reject:{suffix}",
            "supporting_evidence": [suffix], "recommended_future_behavior": "Reject this pattern.",
        }) for suffix in ("one", "two", "old-one", "old-two")]
        for lesson in rejected[2:]:
            growth.decide_lesson(lesson["lesson_id"], "rejected", reviewer="Greg", reason="Prior review.")
        preview = ReviewWorkspaceService(self.root).batch_preview(
            "growth_lesson", rejected[0]["lesson_id"]
        )
        self.client.post(
            f"/review/growth_lesson/{rejected[0]['lesson_id']}/batch/decision",
            data={"snapshot_fingerprint": preview["snapshot_fingerprint"],
                  "decision": "reject", "reason": "Same rejected lesson pattern.",
                  "confirmed": "yes"},
        )
        state = self.store.load()
        self.assertTrue(all(
            state["growth"]["lessons"][lesson["lesson_id"]]["status"] == "rejected"
            for lesson in rejected[:2]
        ))

    def test_batch_routes_require_authenticated_reviewer(self):
        self.save_routine_curator_group()
        app.config.update(
            TESTING=False, AUTH_TEST_BYPASS=False,
            GNOJO_STABLE_SESSION_SECRET_CONFIGURED=True,
            GNOJO_REVIEWER_USERNAME="reviewer",
            GNOJO_REVIEWER_PASSWORD_HASH="not-a-valid-hash",
        )
        anonymous = app.test_client()
        before = (self.root / "curation_memory/memory.json").read_bytes()
        self.assertEqual(
            anonymous.get("/review/curator_task/GKT-A/batch").status_code, 302
        )
        self.assertEqual(
            anonymous.post("/review/curator_task/GKT-A/batch/decision", data={}).status_code,
            403,
        )
        self.assertEqual((self.root / "curation_memory/memory.json").read_bytes(), before)
        app.config.update(TESTING=True, AUTH_TEST_BYPASS=True)


if __name__ == "__main__":
    unittest.main()
