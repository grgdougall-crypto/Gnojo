import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
import re
from pathlib import Path
from unittest.mock import patch

from app.app import app, troubleshooting_session_environment
from app.services.troubleshooting_history_service import TroubleshootingHistoryService


class TroubleshootingHistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = TroubleshootingHistoryService(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_session_lifecycle_and_analytics(self):
        first = self.service.start(
            "internet", "Internet Connection", "check_scope",
            device={"id": "device", "name": "Work laptop", "platform": "Windows"},
            learning_mode=True,
        )
        self.service.progress(first["id"], "check_connection")
        self.service.progress(first["id"], "check_scope", action="back")
        completed = self.service.complete(first["id"], "resolved", "Connection restored")
        self.service.add_feedback(first["id"], {
            "solved": "yes",
            "clarity": 4,
            "confusing_step": "check_scope",
            "comment": "The first question could use an example.",
        })

        second = self.service.start("printer", "Printer", "start")
        self.service.abandon(second["id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["backtracks"], 1)
        self.assertEqual(completed["device"]["name"], "Work laptop")
        analytics = self.service.analytics()
        self.assertEqual(analytics["total"], 2)
        self.assertEqual(analytics["completed"], 1)
        self.assertEqual(analytics["abandoned"], 1)
        self.assertEqual(analytics["completion_rate"], 50)
        self.assertEqual(analytics["feedback_count"], 1)
        self.assertEqual(analytics["solved_rate"], 100)
        self.assertEqual(analytics["average_clarity"], 4.0)
        self.assertEqual(analytics["confusing_steps"][0]["node_id"], "check_scope")
        self.assertEqual(first["session_environment"], "production")

    def test_feedback_requires_completed_session_and_valid_values(self):
        record = self.service.start("internet", "Internet", "start")
        with self.assertRaises(ValueError):
            self.service.add_feedback(record["id"], {"solved": "yes", "clarity": 5})
        self.service.complete(record["id"], "done")
        with self.assertRaises(ValueError):
            self.service.add_feedback(record["id"], {"solved": "maybe", "clarity": 8})

    def test_history_ids_are_validated_and_records_can_be_deleted(self):
        record = self.service.start("internet", "Internet", "start")
        self.service.delete(record["id"])
        self.assertIsNone(self.service.get(record["id"]))
        with self.assertRaises(ValueError):
            self.service.get("../private")

    def _record(self, index, *, workflow="internet", status="completed", days_ago=0,
                environment="production"):
        record = self.service.start(
            workflow,
            "Internet Connection" if workflow == "internet" else "Printer",
            "start",
            session_environment=environment,
        )
        if status == "completed":
            record = self.service.complete(record["id"], "done")
        elif status == "abandoned":
            record = self.service.abandon(record["id"])
        stamp = (
            datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            - timedelta(days=days_ago, seconds=index if days_ago == 0 else 0)
        ).isoformat()
        record["started_at"] = stamp
        record["updated_at"] = stamp
        self.service._write(self.service._path(record["id"]), record)
        return record

    def test_query_page_is_newest_first_sized_and_clamps_invalid_pages(self):
        created = [self._record(index) for index in range(27)]
        first = self.service.query_page(page=1, page_size=20)
        second = self.service.query_page(page=2, page_size=20)
        final = self.service.query_page(page=999, page_size=20)
        invalid = self.service.query_page(page="bad", page_size=20)
        negative = self.service.query_page(page=-4, page_size=20)
        self.assertEqual(len(first["records"]), 20)
        self.assertEqual(len(second["records"]), 7)
        self.assertEqual(first["records"][0]["id"], created[0]["id"])
        self.assertEqual(second["records"][0]["id"], created[20]["id"])
        self.assertEqual(final["page"], 2)
        self.assertEqual(invalid["page"], 1)
        self.assertEqual(negative["page"], 1)

    def test_query_filters_compose_and_date_ranges_are_inclusive(self):
        self._record(0, workflow="internet", status="completed", days_ago=0)
        self._record(1, workflow="internet", status="abandoned", days_ago=7)
        self._record(2, workflow="printer", status="completed", days_ago=8)
        self._record(3, workflow="internet", status="completed", days_ago=30)
        self._record(4, workflow="internet", status="completed", days_ago=31)
        now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)

        seven = self.service.query_page(range="7d", now=now)
        thirty = self.service.query_page(range="30d", now=now)
        all_time = self.service.query_page(range="all", now=now)
        combined = self.service.query_page(
            workflow="INTERNET", status="completed", range="30d", now=now
        )
        self.assertEqual(seven["total_matching"], 2)
        self.assertEqual(thirty["total_matching"], 4)
        self.assertEqual(all_time["total_matching"], 5)
        self.assertEqual(combined["total_matching"], 2)
        self.assertEqual(combined["filters"]["workflow"], "internet")
        self.assertEqual(combined["filters"]["status"], "completed")
        self.assertEqual(self.service.query_page(range="invalid")["filters"]["range"], "all")
        self.assertEqual(self.service.query_page(workflow="missing")["total_matching"], 5)

    def test_analytics_are_global_and_independent_of_page_size_and_filters(self):
        for index in range(30):
            self._record(index, workflow="internet" if index < 10 else "printer")
        small = self.service.query_page(page=1, page_size=5, workflow="internet")
        large = self.service.query_page(page=2, page_size=20, status="abandoned")
        self.assertEqual(small["analytics"], large["analytics"])
        self.assertEqual(small["analytics"]["total"], 30)
        self.assertEqual(small["total_matching"], 10)
        self.assertEqual(large["total_matching"], 0)

    def test_environment_scopes_filter_before_analytics_and_compose(self):
        self._record(0, environment="production", status="completed")
        self._record(1, environment="production", status="abandoned")
        self._record(2, environment="development", status="completed")
        self._record(3, environment="test", status="completed")
        legacy = self._record(4, environment="production", status="completed")
        path = self.service._path(legacy["id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("session_environment")
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

        production = self.service.query_page(environment="production")
        development = self.service.query_page(environment="development")
        test = self.service.query_page(environment="test")
        unclassified = self.service.query_page(environment="unclassified")
        all_records = self.service.query_page(environment="all")

        self.assertEqual(production["total_scoped"], 2)
        self.assertEqual(production["analytics"]["total"], 2)
        self.assertEqual(production["analytics"]["completion_rate"], 50)
        self.assertEqual(development["analytics"]["total"], 1)
        self.assertEqual(test["analytics"]["total"], 1)
        self.assertEqual(unclassified["analytics"]["total"], 1)
        self.assertEqual(all_records["analytics"]["total"], 5)
        self.assertEqual(
            self.service.query_page(
                environment="production", status="completed"
            )["total_matching"],
            1,
        )
        self.assertEqual(
            self.service.query_page(environment="invalid")["filters"]["environment"],
            "production",
        )

    def test_legacy_record_projects_unclassified_without_rewrite(self):
        record = self.service.start("internet", "Internet", "start")
        path = self.service._path(record["id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("session_environment")
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        before = path.read_bytes()

        projected = self.service.get(record["id"])

        self.assertEqual(projected["session_environment"], "unclassified")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            self.service.list(environment="unclassified")[0]["id"], record["id"]
        )
        self.service.progress(record["id"], "next")
        self.assertNotIn(
            "session_environment",
            json.loads(path.read_text(encoding="utf-8")),
        )

    def test_invalid_new_session_environment_is_rejected_without_record(self):
        with self.assertRaises(ValueError):
            self.service.start(
                "internet", "Internet", "start", session_environment="staging"
            )
        self.assertEqual(self.service.list(), [])

    def test_deletion_that_removes_final_page_clamps_to_new_last_page(self):
        records = [self._record(index) for index in range(21)]
        self.assertEqual(self.service.query_page(page=2, page_size=20)["page"], 2)
        self.service.delete(records[-1]["id"])
        page = self.service.query_page(page=2, page_size=20)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["total_pages"], 1)


class TroubleshootingHistoryPageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = TroubleshootingHistoryService(Path(self.temporary.name))
        self.service_patch = patch(
            "app.app.TroubleshootingHistoryService", return_value=self.service
        )
        self.service_patch.start()
        self.previous_testing = app.config.get("TESTING")
        self.previous_debug = app.config.get("DEBUG")
        self.previous_environment = os.environ.get("GNOJO_SESSION_ENVIRONMENT")
        os.environ["GNOJO_SESSION_ENVIRONMENT"] = ""
        app.config.update(TESTING=True, DEBUG=False)
        self.client = app.test_client()

    def tearDown(self):
        app.config.update(TESTING=self.previous_testing, DEBUG=self.previous_debug)
        if self.previous_environment is None:
            os.environ.pop("GNOJO_SESSION_ENVIRONMENT", None)
        else:
            os.environ["GNOJO_SESSION_ENVIRONMENT"] = self.previous_environment
        self.service_patch.stop()
        self.temporary.cleanup()

    def test_wizard_start_creates_private_local_history_and_page(self):
        response = self.client.get("/wizard?workflow=internet")
        self.assertEqual(response.status_code, 200)
        records = self.service.list()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "active")
        self.assertEqual(records[0]["session_environment"], "test")
        self.assertNotIn("answers", records[0])

        history = self.client.get("/troubleshooting-history?environment=test")
        html = history.get_data(as_text=True)
        self.assertEqual(history.status_code, 200)
        self.assertIn("Troubleshooting History", html)
        self.assertIn("Internet Connection", html)
        self.assertIn("not the answers you enter", html)

        detail = self.client.get(
            f"/troubleshooting-history/{records[0]['id']}"
        )
        self.assertIn("Session Path", detail.get_data(as_text=True))
        self.assertIn("Environment", detail.get_data(as_text=True))
        self.assertIn("Test", detail.get_data(as_text=True))

    def test_creation_environment_resolution_precedence_and_safe_fallback(self):
        for configured in ("production", "development", "test"):
            with self.subTest(configured=configured):
                os.environ["GNOJO_SESSION_ENVIRONMENT"] = configured
                app.config.update(TESTING=True, DEBUG=True)
                self.assertEqual(troubleshooting_session_environment(), configured)

        os.environ["GNOJO_SESSION_ENVIRONMENT"] = ""
        app.config.update(TESTING=True, DEBUG=False)
        self.assertEqual(troubleshooting_session_environment(), "test")
        app.config.update(TESTING=False, DEBUG=True)
        self.assertEqual(troubleshooting_session_environment(), "development")
        app.config.update(TESTING=False, DEBUG=False)
        self.assertEqual(troubleshooting_session_environment(), "production")
        os.environ["GNOJO_SESSION_ENVIRONMENT"] = "invalid"
        app.config.update(TESTING=False, DEBUG=False)
        self.assertEqual(troubleshooting_session_environment(), "production")
        os.environ["GNOJO_SESSION_ENVIRONMENT"] = ""

    def test_explicit_environments_are_persisted_by_canonical_wizard_start(self):
        for configured in ("production", "development", "test"):
            with self.subTest(configured=configured):
                os.environ["GNOJO_SESSION_ENVIRONMENT"] = configured
                self.service.clear()
                with self.client.session_transaction() as browser_session:
                    browser_session.clear()
                response = self.client.get("/wizard?workflow=internet")
                self.assertEqual(response.status_code, 200)
                record = self.service.list()[0]
                self.assertEqual(record["session_environment"], configured)
        os.environ["GNOJO_SESSION_ENVIRONMENT"] = ""

    def test_restarting_marks_previous_session_abandoned(self):
        self.client.get("/wizard?workflow=internet")
        self.client.get("/wizard?workflow=internet&restart=1")
        records = self.service.list()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(item["status"] for item in records), ["abandoned", "active"]
        )

    def test_reaching_resolution_completes_history_record(self):
        self.client.get("/wizard?workflow=internet")
        for answer in ("yes", "wifi", "yes", "", "yes"):
            response = self.client.post("/wizard", data={"answer": answer}, follow_redirects=True)
        record = self.service.list()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["final_node_id"], "resolved")
        self.assertEqual(record["outcome"], "Internet Connection Restored")
        self.assertGreaterEqual(record["steps"], 6)
        self.assertIn("How did this workflow perform?", response.get_data(as_text=True))

        feedback = self.client.post(
            f"/api/troubleshooting-history/{record['id']}/feedback",
            json={
                "solved": "partially",
                "clarity": 3,
                "confusing_step": "verify_resolution",
                "comment": "A little more detail would help.",
            },
        )
        self.assertEqual(feedback.status_code, 200)
        saved = self.service.get(record["id"])["feedback"]
        self.assertEqual(saved["solved"], "partially")
        self.assertEqual(saved["clarity"], 3)

        history_html = self.client.get(
            "/troubleshooting-history?environment=test"
        ).get_data(as_text=True)
        self.assertIn("Workflow Feedback", history_html)
        self.assertIn("Average clarity", history_html)
        detail_html = self.client.get(
            f"/troubleshooting-history/{record['id']}"
        ).get_data(as_text=True)
        self.assertIn("Workflow Quality", detail_html)
        self.assertIn("A little more detail would help.", detail_html)

    def test_page_filters_pagination_urls_and_accessible_detail_names(self):
        for index in range(26):
            record = self.service.start("internet", "Internet Connection", "start")
            self.service.complete(record["id"], "done")
        html = self.client.get(
            "/troubleshooting-history?workflow=internet&status=completed&range=30d"
        ).get_data(as_text=True)
        self.assertEqual(html.count("View details</a>"), 25)
        self.assertIn("Showing 26 of 26 sessions in this environment.", html)
        self.assertIn("Page 1 of 2", html)
        self.assertIn("page=2&amp;environment=production&amp;workflow=internet", html)
        self.assertIn("Clear filters", html)
        self.assertIn("aria-label=\"View Internet Connection session details from", html)
        self.assertIn(
            "return_to=%2Ftroubleshooting-history%3Fworkflow%3Dinternet%26status%3Dcompleted%26range%3D30d",
            html,
        )
        page_two = self.client.get(
            "/troubleshooting-history?page=2&environment=production&workflow=internet&status=completed&range=30d"
        ).get_data(as_text=True)
        self.assertEqual(page_two.count("View details</a>"), 1)
        self.assertIn('aria-current="page">Page 2 of 2', page_two)

        detail_url = unescape(re.search(
            r'href="([^"]+\?return_to=[^"]+)"[^>]+aria-label="View Internet Connection session details',
            page_two,
        ).group(1))
        self.assertIn(
            "return_to=%2Ftroubleshooting-history%3Fpage%3D2%26environment%3Dproduction%26workflow%3Dinternet%26status%3Dcompleted%26range%3D30d",
            detail_url,
        )
        detail = self.client.get(detail_url).get_data(as_text=True)
        self.assertIn(
            'href="/troubleshooting-history?page=2&amp;environment=production&amp;workflow=internet&amp;status=completed&amp;range=30d"',
            detail,
        )
        self.assertIn("Back to Troubleshooting History", detail)
        back_url = unescape(re.search(
            r'<a class="back-link" href="([^"]+)"', detail
        ).group(1))
        returned = self.client.get(back_url).get_data(as_text=True)
        self.assertIn('value="internet" selected', returned)
        self.assertIn('value="completed" selected', returned)
        self.assertIn('value="30d" selected', returned)
        self.assertIn('value="production" selected', returned)
        self.assertIn('aria-current="page">Page 2 of 2', returned)

        record = self.service.list()[0]
        unsafe = self.client.get(
            f"/troubleshooting-history/{record['id']}?return_to=https%3A%2F%2Fevil.example"
        ).get_data(as_text=True)
        self.assertIn('href="/troubleshooting-history"', unsafe)
        self.assertNotIn("evil.example", unsafe)

    def test_page_distinguishes_no_history_from_filtered_no_match(self):
        empty = self.client.get("/troubleshooting-history").get_data(as_text=True)
        self.assertIn("No Troubleshooting History Yet", empty)
        self.assertNotIn("No sessions match these filters", empty)

        self.service.start("internet", "Internet Connection", "start")
        filtered = self.client.get(
            "/troubleshooting-history?status=completed"
        ).get_data(as_text=True)
        self.assertIn("No sessions match these filters", filtered)
        self.assertIn("Clear filters", filtered)
        self.assertNotIn("No Troubleshooting History Yet", filtered)

        self.service.clear()
        self.service.start(
            "internet", "Internet Connection", "start", session_environment="test"
        )
        scoped = self.client.get("/troubleshooting-history").get_data(as_text=True)
        self.assertIn("No sessions in this environment", scoped)
        self.assertNotIn("No Troubleshooting History Yet", scoped)

    def test_analytics_discloses_unclassified_local_test_sessions(self):
        html = self.client.get("/troubleshooting-history").get_data(as_text=True)
        self.assertIn("Analytics scope", html)
        self.assertIn("Older sessions without authoritative classification", html)
        self.assertIn("Unclassified", html)


if __name__ == "__main__":
    unittest.main()
