import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from curator.auditor import CuratorAuditor
from curator.checks import CuratorChecks
from curator.models import AuditFilter, InventoryRecord
from curator.tasks import KnowledgeTaskService
from curator.workflow_reasoning import WorkflowReasoningAuditor


class WorkflowReasoningAuditorTests(unittest.TestCase):
    def setUp(self):
        self.auditor = WorkflowReasoningAuditor()

    @staticmethod
    def workflow(nodes, start="q", steps=5):
        return {"workflow_id": "fixture", "name": "Fixture", "description": "A deterministic reasoning fixture.",
                "category": "Networking", "platform": "Windows", "estimated_steps": steps,
                "start_node": start, "nodes": nodes}

    @staticmethod
    def rules(observations):
        return [item.rule for item in observations]

    def test_early_convergence_is_detected(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which resource?", "answers": {
                "cpu": {"label": "CPU", "next": "cpu_check"}, "disk": {"label": "Disk", "next": "disk_check"}}},
            "cpu_check": {"type": "instruction", "title": "Inspect CPU", "next": "shared"},
            "disk_check": {"type": "instruction", "title": "Inspect Disk", "next": "shared"},
            "shared": {"type": "resolution", "title": "Additional Diagnostics Required"},
        })
        finding = next(item for item in self.auditor.analyze(workflow) if item.rule == "CUR-WR-EARLY-CONVERGENCE")
        self.assertEqual(finding.structural["convergence_node"], "shared")
        self.assertEqual(finding.classification, "opportunity")

    def test_distinct_branches_that_do_not_converge_are_not_flagged(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which?", "answers": {
                "cpu": {"label": "CPU", "next": "cpu_done"}, "disk": {"label": "Disk", "next": "disk_done"}}},
            "cpu_done": {"type": "resolution", "title": "CPU Pressure Observed"},
            "disk_done": {"type": "resolution", "title": "Disk Pressure Observed"},
        })
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def remediation_rejoin_workflow(self, *, question, action_id, action_title, convergence):
        return self.workflow({
            "q": {"type": "question", "question": question, "answers": {
                "yes": {"label": "Yes", "next": action_id},
                "no": {"label": "No", "next": convergence},
            }},
            action_id: {"type": "instruction", "title": action_title,
                        "instruction": action_title, "next": "verify"},
            "verify": {"type": "question", "question": "Did the action improve the condition?", "answers": {
                "yes": {"label": "Yes", "next": "resolved"},
                "no": {"label": "No", "next": convergence},
            }},
            convergence: {"type": "instruction", "title": "Inspect the next diagnostic area", "next": "done"},
            "resolved": {"type": "resolution", "title": "Condition Improved"},
            "done": {"type": "resolution", "title": "Review Complete"},
        })

    def assert_remediation_rejoin_suppressed(self, workflow):
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_restart_if_needed_rejoin_is_suppressed(self):
        self.assert_remediation_rejoin_suppressed(self.remediation_rejoin_workflow(
            question="Has the computer already been restarted?", action_id="restart_windows",
            action_title="Restart Windows safely", convergence="inspect_task_manager"))

    def test_low_storage_remediation_rejoin_is_suppressed(self):
        self.assert_remediation_rejoin_suppressed(self.remediation_rejoin_workflow(
            question="Is the Windows drive nearly full?", action_id="safe_storage_cleanup",
            action_title="Cleanup unnecessary storage", convergence="review_startup_apps"))

    def test_install_updates_if_needed_rejoin_is_suppressed(self):
        self.assert_remediation_rejoin_suppressed(self.remediation_rejoin_workflow(
            question="Are Windows updates pending?", action_id="install_updates",
            action_title="Install approved Windows updates", convergence="escalate_performance"))

    def test_change_startup_apps_restart_rejoin_is_suppressed(self):
        self.assert_remediation_rejoin_suppressed(self.remediation_rejoin_workflow(
            question="Were optional startup applications changed?", action_id="restart_after_startup",
            action_title="Restart after changing startup applications", convergence="run_security_scan"))

    def test_distinct_signals_immediately_converging_are_still_flagged(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which resource is constrained?", "answers": {
                "cpu": {"label": "CPU", "next": "inspect_cpu"},
                "disk": {"label": "Disk", "next": "inspect_disk"},
            }},
            "inspect_cpu": {"type": "instruction", "title": "Inspect CPU", "next": "shared"},
            "inspect_disk": {"type": "instruction", "title": "Inspect Disk", "next": "shared"},
            "shared": {"type": "resolution", "title": "Additional Diagnostics Required"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_generic_informational_paths_do_not_suppress_convergence(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which signal was observed?", "answers": {
                "a": {"label": "Signal A", "next": "info_a"},
                "b": {"label": "Signal B", "next": "info_b"},
            }},
            "info_a": {"type": "instruction", "title": "Read signal A information", "next": "shared"},
            "info_b": {"type": "instruction", "title": "Read signal B information", "next": "shared"},
            "shared": {"type": "resolution", "title": "Additional Diagnostics Required"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_non_rejoining_remediation_is_not_treated_as_rejoin_pattern(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Is remediation needed?", "answers": {
                "yes": {"label": "Yes", "next": "restart"},
                "no": {"label": "No", "next": "inspect"},
            }},
            "restart": {"type": "instruction", "title": "Restart Windows", "next": "restart_done"},
            "restart_done": {"type": "resolution", "title": "Restart Complete"},
            "inspect": {"type": "instruction", "title": "Inspect CPU", "next": "inspect_done"},
            "inspect_done": {"type": "resolution", "title": "Inspection Complete"},
        })
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_remediation_path_that_reenters_origin_question_is_not_suppressed(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Is a warning displayed?", "answers": {
                "yes": {"label": "Yes", "next": "clear_warning"},
                "no": {"label": "No", "next": "advanced"},
            }},
            "clear_warning": {"type": "instruction", "title": "Clear the warning", "next": "verify"},
            "verify": {"type": "question", "question": "Did clearing the warning work?", "answers": {
                "yes": {"label": "Yes", "next": "resolved"},
                "no": {"label": "No", "next": "q"},
            }},
            "resolved": {"type": "resolution", "title": "Warning Cleared"},
            "advanced": {"type": "resolution", "title": "Additional Diagnostics Required"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_multi_branch_question_is_not_broadly_suppressed(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "What state applies?", "answers": {
                "yes": {"label": "Needs restart", "next": "restart"},
                "no": {"label": "No restart needed", "next": "shared"},
                "other": {"label": "Different signal", "next": "info"},
            }},
            "restart": {"type": "instruction", "title": "Restart Windows", "next": "shared"},
            "info": {"type": "instruction", "title": "Review different signal", "next": "shared"},
            "shared": {"type": "resolution", "title": "Additional Diagnostics Required"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_branch_specific_handling_to_shared_verification_is_suppressed(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which connection is in use?", "answers": {
                "wired": {"label": "Wired", "next": "check_cable"},
                "wireless": {"label": "Wireless", "next": "restart_radio"}}},
            "check_cable": {"type": "instruction", "title": "Check the cable", "instruction": "Reconnect the cable.", "next": "verify"},
            "restart_radio": {"type": "instruction", "title": "Restart the radio", "instruction": "Restart the radio.", "next": "verify"},
            "verify": {"type": "question", "question": "Does the connection work now?", "answers": {"yes": {"next": "done"}}},
            "done": {"type": "resolution", "title": "Connected"},
        })
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_nested_branch_specific_handling_to_shared_verification_is_suppressed(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Is the device powered?", "answers": {
                "yes": {"label": "Yes", "next": "transport"}, "no": {"label": "No", "next": "power_on"}}},
            "transport": {"type": "question", "question": "Which transport?", "answers": {
                "a": {"label": "Cable", "next": "check_cable"}, "b": {"label": "Network", "next": "restart_device"}}},
            "check_cable": {"type": "instruction", "title": "Check the cable", "instruction": "Reconnect the cable.", "next": "verify"},
            "restart_device": {"type": "instruction", "title": "Restart device", "next": "verify"},
            "power_on": {"type": "instruction", "title": "Turn on the device", "next": "verify"},
            "verify": {"type": "question", "question": "Can the device operate now?", "answers": {"yes": {"next": "done"}}},
            "done": {"type": "resolution", "title": "Operational"},
        })
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_success_vs_meaningful_continued_troubleshooting_is_suppressed(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Does it work now?", "answers": {
                "yes": {"label": "Yes", "next": "resolved"}, "no": {"label": "No", "next": "restart"}}},
            "restart": {"type": "instruction", "title": "Restart the service", "next": "resolved"},
            "resolved": {"type": "resolution", "title": "Service Restored"},
        })
        self.assertNotIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_success_vs_generic_continuation_is_still_reported(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Does it work now?", "answers": {
                "yes": {"label": "Yes", "next": "resolved"}, "no": {"label": "No", "next": "information"}}},
            "information": {"type": "instruction", "title": "Read additional information", "instruction": "Read the guidance.", "next": "resolved"},
            "resolved": {"type": "resolution", "title": "Review Complete"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_unhandled_routes_to_common_verification_are_still_reported(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which state?", "answers": {
                "a": {"label": "State A", "next": "note_a"}, "b": {"label": "State B", "next": "note_b"}}},
            "note_a": {"type": "instruction", "title": "Read state A information", "next": "verify"},
            "note_b": {"type": "instruction", "title": "Read state B information", "next": "verify"},
            "verify": {"type": "question", "question": "Does the issue remain?", "answers": {"yes": {"next": "done"}}},
            "done": {"type": "resolution", "title": "Review Complete"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_ambiguous_question_to_common_terminal_is_still_reported(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which option?", "answers": {
                "a": {"label": "Option A", "next": "restart_a"}, "b": {"label": "Option B", "next": "restart_b"}}},
            "restart_a": {"type": "instruction", "title": "Restart A", "next": "done"},
            "restart_b": {"type": "instruction", "title": "Restart B", "next": "done"},
            "done": {"type": "resolution", "title": "Review Complete"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_success_pattern_that_loops_through_origin_is_still_reported(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Does it work now?", "answers": {
                "yes": {"label": "Yes", "next": "resolved"}, "no": {"label": "No", "next": "restart"}}},
            "restart": {"type": "instruction", "title": "Restart the service", "next": "q"},
            "resolved": {"type": "resolution", "title": "Service Restored"},
        })
        self.assertIn("CUR-WR-EARLY-CONVERGENCE", self.rules(self.auditor.analyze(workflow)))

    def test_suppression_does_not_change_strong_signal_rule(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which signal?", "answers": {
                "cpu": {"label": "CPU", "next": "generic"},
                "memory": {"label": "Memory", "next": "generic"},
            }},
            "generic": {"type": "resolution", "title": "Deeper Diagnostics Recommended"},
        })
        findings = self.auditor.analyze(workflow)
        self.assertIn("CUR-WR-SIGNAL-RETENTION", self.rules(findings))

    def test_strong_signal_loss_is_detected(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Which resource?", "answers": {
                "cpu": {"label": "CPU", "next": "generic"}, "memory": {"label": "Memory", "next": "generic"}}},
            "generic": {"type": "resolution", "title": "Deeper Diagnostics Recommended"},
        })
        finding = next(item for item in self.auditor.analyze(workflow) if item.rule == "CUR-WR-SIGNAL-RETENTION")
        self.assertEqual(finding.structural["signals"], ["CPU", "Memory"])

    def test_action_followed_by_verification_is_not_flagged(self):
        workflow = self.workflow({
            "q": {"type": "instruction", "title": "Restart Application", "instruction": "Restart the application.", "next": "verify"},
            "verify": {"type": "question", "question": "Did performance improve?", "answers": {"yes": {"next": "done"}}},
            "done": {"type": "resolution", "title": "Resolved"},
        })
        self.assertNotIn("CUR-WR-ACTION-VERIFICATION", self.rules(self.auditor.analyze(workflow)))

    def test_action_without_verification_is_flagged(self):
        workflow = self.workflow({
            "q": {"type": "instruction", "title": "Restart Application", "instruction": "Restart the application.", "next": "other"},
            "other": {"type": "instruction", "title": "Inspect Storage", "instruction": "View storage.", "next": "done"},
            "done": {"type": "resolution", "title": "Additional Review"},
        })
        self.assertIn("CUR-WR-ACTION-VERIFICATION", self.rules(self.auditor.analyze(workflow)))

    def dns_workflow(self, include_external):
        nodes = {
            "q": {"type": "instruction", "title": "Test Default Gateway", "instruction": "Ping the default gateway.", "next": "gateway"},
            "gateway": {"type": "question", "question": "Did the gateway respond?", "answers": {"yes": {"next": "external" if include_external else "dns"}}},
            "dns": {"type": "instruction", "title": "Test DNS Resolution", "instruction": "Run nslookup example.com.", "next": "dns_result"},
            "dns_result": {"type": "question", "question": "Did nslookup return an address?", "answers": {"no": {"next": "dns_problem"}}},
            "dns_problem": {"type": "resolution", "title": "DNS Resolution Problem"},
        }
        if include_external:
            nodes["external"] = {"type": "instruction", "title": "Test External IP Reachability", "instruction": "Test upstream reachability using a public IP.", "next": "dns"}
        return self.workflow(nodes)

    def test_registered_terminal_requirement_detects_missing_evidence(self):
        findings = [item for item in self.auditor.analyze(self.dns_workflow(False)) if item.rule == "CUR-WR-TERMINAL-EVIDENCE"]
        self.assertEqual(len(findings), 1)
        self.assertIn("external_ip_reachability", findings[0].structural["missing"])
        self.assertEqual(findings[0].structural["affected_paths"], [{
            "nodes": ["q", "gateway", "dns", "dns_result", "dns_problem"],
            "missing": ["external_ip_reachability"],
            "predecessor_edge": {
                "source": "dns_result", "route": "no", "destination": "dns_problem",
            },
        }])
        self.assertEqual(findings[0].structural["predecessor_edges"], [{
            "source": "dns_result", "route": "no", "destination": "dns_problem",
        }])

    def test_terminal_evidence_finding_and_task_identity_ignore_enriched_evidence(self):
        workflow = self.dns_workflow(False)
        record = InventoryRecord(
            "workflow", "fixture", "Fixture", "app/workflow_drafts/fixture.json",
            "Networking", "Windows", "draft", workflow,
        )
        first = next(item for item in CuratorChecks().run_record(record)
                     if item.rule == "CUR-WR-TERMINAL-EVIDENCE")
        second = next(item for item in CuratorChecks().run_record(record)
                      if item.rule == "CUR-WR-TERMINAL-EVIDENCE")

        self.assertEqual(first.identifier, second.identifier)
        self.assertEqual(
            KnowledgeTaskService.task_id(KnowledgeTaskService.durable_identity(first)),
            KnowledgeTaskService.task_id(KnowledgeTaskService.durable_identity(second)),
        )
        self.assertEqual(first.structured_evidence["terminal"], "dns_problem")
        state = {"tasks": {}}
        service = KnowledgeTaskService()
        first_result = service.reconcile(
            state, [first], [record], run_id="AUD-1", observed_at="2026-08-24T00:00:00+00:00",
            filters=AuditFilter(),
        )
        second_result = service.reconcile(
            state, [second], [record], run_id="AUD-2", observed_at="2026-08-24T01:00:00+00:00",
            filters=AuditFilter(),
        )
        task_id = first_result["created"][0]
        self.assertEqual(second_result["created"], [])
        self.assertEqual(list(state["tasks"]), [task_id])
        self.assertEqual(state["tasks"][task_id]["structured_evidence"]["predecessor_edges"], [{
            "source": "dns_result", "route": "no", "destination": "dns_problem",
        }])

    def test_registered_terminal_requirement_passes_with_evidence(self):
        self.assertNotIn("CUR-WR-TERMINAL-EVIDENCE", self.rules(self.auditor.analyze(self.dns_workflow(True))))

    def test_current_dns_reference_workflow_is_flagged(self):
        workflow = json.loads(Path("app/workflow_drafts/network_diagnostics.json").read_text(encoding="utf-8"))
        findings = [item for item in self.auditor.analyze(workflow) if item.rule == "CUR-WR-TERMINAL-EVIDENCE"]
        self.assertEqual([item.node_id for item in findings], ["dns_problem"])

    def test_current_performance_reference_retains_signal_finding(self):
        workflow = json.loads(Path("app/workflow_drafts/windows_slow.json").read_text(encoding="utf-8"))
        finding = next(item for item in self.auditor.analyze(workflow) if item.rule == "CUR-WR-SIGNAL-RETENTION")
        self.assertEqual(finding.node_id, "identify_bottleneck")
        self.assertIn("Memory", finding.structural["signals"])

    def test_cycle_is_bounded(self):
        workflow = self.workflow({
            "q": {"type": "question", "question": "Continue?", "answers": {"yes": {"next": "a"}, "no": {"next": "done"}}},
            "a": {"type": "instruction", "title": "Inspect", "next": "q"},
            "done": {"type": "resolution", "title": "Done"},
        })
        self.assertIsInstance(self.auditor.analyze(workflow), list)

    def test_repeated_analysis_has_stable_nonduplicate_observations(self):
        workflow = self.dns_workflow(False)
        first = self.auditor.analyze(workflow)
        second = self.auditor.analyze(workflow)
        self.assertEqual(first, second)
        self.assertEqual(len({(item.rule, item.node_id) for item in first}), len(first))

    def test_actionable_lifecycle_copy_only_is_reasoned_about(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/decision_trees").mkdir(parents=True)
            (root / "app/workflow_drafts").mkdir(parents=True)
            built_in = self.dns_workflow(False)
            draft = self.dns_workflow(True)
            for value in (built_in, draft):
                value["workflow_id"] = "fixture"
            (root / "app/decision_trees/fixture.json").write_text(json.dumps(built_in), encoding="utf-8")
            (root / "app/workflow_drafts/fixture.json").write_text(json.dumps(draft), encoding="utf-8")
            built_record = InventoryRecord("workflow", "fixture", "Fixture", "app/decision_trees/fixture.json", "Networking", "Windows", "built_in", built_in)
            draft_record = InventoryRecord("workflow", "fixture", "Fixture", "app/workflow_drafts/fixture.json", "Networking", "Windows", "draft", draft)
            checks = CuratorChecks(root)
            self.assertFalse(any(item.rule.startswith("CUR-WR-") for item in checks.run_record(built_record)))
            self.assertFalse(any(item.rule == "CUR-WR-TERMINAL-EVIDENCE" for item in checks.run_record(draft_record)))

    def test_reasoning_findings_reconcile_into_open_human_review_tasks(self):
        workflow = self.dns_workflow(False)
        record = InventoryRecord("workflow", "fixture", "Fixture", "fixture.json", "Networking", "Windows", "draft", workflow)
        findings = [item for item in CuratorChecks(Path.cwd()).run_record(record) if item.rule.startswith("CUR-WR-")]
        state = {"tasks": {}}
        result = KnowledgeTaskService().reconcile(state, findings, [record], run_id="RUN-1", observed_at="2026-01-01T00:00:00+00:00", filters=AuditFilter(content_type="workflow"))
        tasks = [state["tasks"][task_id] for task_id in result["created"]]
        self.assertTrue(tasks)
        self.assertTrue(all(task["status"] == "open" and task["execution_mode"] == "ASSISTED" for task in tasks))
        second = KnowledgeTaskService().reconcile(state, findings, [record], run_id="RUN-2", observed_at="2026-01-02T00:00:00+00:00", filters=AuditFilter(content_type="workflow"))
        self.assertFalse(second["created"])

    def test_reasoning_task_is_not_automatically_resolved_when_finding_disappears(self):
        workflow = self.dns_workflow(False)
        record = InventoryRecord("workflow", "fixture", "Fixture", "fixture.json", "Networking", "Windows", "draft", workflow)
        findings = [item for item in CuratorChecks(Path.cwd()).run_record(record) if item.rule.startswith("CUR-WR-")]
        state = {"tasks": {}}
        created = KnowledgeTaskService().reconcile(
            state, findings, [record], run_id="RUN-1", observed_at="2026-01-01T00:00:00+00:00",
            filters=AuditFilter(content_type="workflow"),
        )
        task_id = created["created"][0]

        KnowledgeTaskService().reconcile(
            state, [], [record], run_id="RUN-2", observed_at="2026-01-02T00:00:00+00:00",
            filters=AuditFilter(content_type="workflow"),
        )

        self.assertNotEqual(state["tasks"][task_id]["status"], "resolved")

    def test_intentional_disposition_survives_when_finding_disappears(self):
        workflow = self.dns_workflow(False)
        record = InventoryRecord("workflow", "fixture", "Fixture", "fixture.json", "Networking", "Windows", "draft", workflow)
        findings = [item for item in CuratorChecks(Path.cwd()).run_record(record) if item.rule.startswith("CUR-WR-")]
        state = {"tasks": {}}
        created = KnowledgeTaskService().reconcile(
            state, findings, [record], run_id="RUN-1", observed_at="2026-01-01T00:00:00+00:00",
            filters=AuditFilter(content_type="workflow"),
        )
        task_id = created["created"][0]
        state["tasks"][task_id]["review_disposition"] = "INTENTIONAL"
        KnowledgeTaskService().reconcile(
            state, [], [record], run_id="RUN-2", observed_at="2026-01-02T00:00:00+00:00",
            filters=AuditFilter(content_type="workflow"),
        )
        self.assertEqual(state["tasks"][task_id]["review_disposition"], "INTENTIONAL")
        self.assertNotEqual(state["tasks"][task_id]["status"], "resolved")

    def test_analysis_does_not_mutate_workflow(self):
        workflow = self.dns_workflow(False)
        before = deepcopy(workflow)
        self.auditor.analyze(workflow)
        self.assertEqual(workflow, before)

    def test_progress_inconsistency_is_detected(self):
        workflow = self.workflow({
            "q": {"type": "instruction", "title": "One", "next": "two"},
            "two": {"type": "instruction", "title": "Two", "next": "three"},
            "three": {"type": "question", "question": "Three?", "answers": {"yes": {"next": "four"}}},
            "four": {"type": "instruction", "title": "Four", "next": "done"},
            "done": {"type": "resolution", "title": "Done"},
        }, steps=2)
        self.assertIn("CUR-WR-PROGRESS", self.rules(self.auditor.analyze(workflow)))


if __name__ == "__main__":
    unittest.main()
