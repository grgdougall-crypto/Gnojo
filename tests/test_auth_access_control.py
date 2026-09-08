import re
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app.app import app
from app.services.authentication_service import ReviewerAccessPolicy


class AuthenticationAccessControlTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "AUTH_TEST_BYPASS",
                "GNOJO_REVIEWER_USERNAME",
                "GNOJO_REVIEWER_PASSWORD_HASH",
                "GNOJO_STABLE_SESSION_SECRET_CONFIGURED",
                "SESSION_COOKIE_SECURE",
                "PERMANENT_SESSION_LIFETIME",
            )
        }
        app.config.update(
            TESTING=True,
            AUTH_TEST_BYPASS=False,
            GNOJO_REVIEWER_USERNAME="reviewer",
            GNOJO_REVIEWER_PASSWORD_HASH=generate_password_hash("correct horse"),
            GNOJO_STABLE_SESSION_SECRET_CONFIGURED=True,
            SESSION_COOKIE_SECURE=False,
        )
        self.client = app.test_client()

    def tearDown(self):
        for key, value in self.original.items():
            if value is None:
                app.config.pop(key, None)
            else:
                app.config[key] = value

    @staticmethod
    def _token(response):
        match = re.search(
            rb'name="authenticity_token" value="([^"]+)"', response.data
        )
        if not match:
            raise AssertionError("Authentication CSRF token was not rendered.")
        return match.group(1).decode()

    def _login(self, next_destination="/content-studio"):
        page = self.client.get(f"/login?next={next_destination}")
        return self.client.post(
            "/login",
            data={
                "authenticity_token": self._token(page),
                "username": "reviewer",
                "password": "correct horse",
                "next": next_destination,
            },
            follow_redirects=False,
        )

    def test_public_demo_pages_remain_available_anonymously(self):
        for path in ("/", "/workflows", "/knowledge", "/knowledge/published", "/commands", "/search"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_protected_get_redirects_to_login_with_local_return(self):
        response = self.client.get("/content-studio?from=home")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "/login?next=/content-studio?from%3Dhome",
        )
        api_response = self.client.get("/api/workflow-drafts/example.json/lifecycle")
        self.assertEqual(api_response.status_code, 302)
        self.assertTrue(api_response.headers["Location"].startswith("/login?next="))

    @patch("app.app.CuratorDashboardService")
    def test_anonymous_protected_post_cannot_mutate(self, dashboard_service):
        response = self.client.post("/curator/run")
        self.assertEqual(response.status_code, 403)
        dashboard_service.assert_not_called()

    def test_valid_login_authenticates_and_protected_route_remains_usable(self):
        response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/content-studio")
        page = self.client.get("/content-studio")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Content Studio", page.data)

    def test_invalid_login_fails_without_authenticating(self):
        page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "authenticity_token": self._token(page),
                "username": "reviewer",
                "password": "wrong",
                "next": "/curator",
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"not recognized", response.data)
        self.assertEqual(self.client.get("/curator").status_code, 302)

    def test_logout_requires_csrf_and_clears_authentication(self):
        self._login()
        authenticated_page = self.client.get("/")
        token = self._token(authenticated_page)
        response = self.client.post(
            "/logout", data={"authenticity_token": token}, follow_redirects=False
        )
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.client.get("/content-studio").status_code, 302)

    def test_safe_and_unsafe_login_returns(self):
        safe = self._login("/curator?status=open")
        self.assertEqual(safe.headers["Location"], "/curator?status=open")

        self.client.post(
            "/logout",
            data={"authenticity_token": self._token(self.client.get("/"))},
        )
        for unsafe in ("https://example.com/steal", "//example.com", r"/\\example.com"):
            with self.subTest(unsafe=unsafe):
                response = self._login(unsafe)
                self.assertEqual(response.headers["Location"], "/content-studio")
                self.client.post(
                    "/logout",
                    data={"authenticity_token": self._token(self.client.get("/"))},
                )

    def test_navigation_is_role_aware(self):
        anonymous = self.client.get("/").get_data(as_text=True)
        self.assertIn("Sign In", anonymous)
        self.assertNotIn('href="/content-studio"', anonymous)

        self._login()
        authenticated = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/content-studio"', authenticated)
        self.assertIn("Sign Out", authenticated)
        self.assertNotIn(">Sign In<", authenticated)

    @patch("app.app.CuratorDashboardService")
    def test_authenticated_privileged_post_requires_csrf(self, dashboard_service):
        self._login()
        response = self.client.post("/curator/run")
        self.assertEqual(response.status_code, 400)
        dashboard_service.assert_not_called()

    @patch("app.app.CuratorDashboardService")
    def test_authenticated_privileged_post_still_works_with_csrf(self, dashboard_service):
        self._login()
        page = self.client.get("/")
        response = self.client.post(
            "/curator/run",
            data={"authenticity_token": self._token(page)},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        dashboard_service.return_value.run_audit.assert_called_once_with()

    def test_anonymous_public_pages_hide_embedded_admin_controls(self):
        knowledge = self.client.get("/knowledge").get_data(as_text=True)
        scripts = self.client.get("/scripts").get_data(as_text=True)
        published = self.client.get("/knowledge/published").get_data(as_text=True)
        self.assertNotIn("Open Command Builder", knowledge)
        self.assertNotIn("Review Drafts", knowledge)
        self.assertNotIn("Add script", scripts)
        self.assertNotIn("Manage in Integrity", published)

    def test_local_development_cookie_is_not_forced_secure(self):
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        response = self._login()
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("; Secure", cookie)

    def test_missing_credentials_fail_closed(self):
        app.config.update(
            GNOJO_REVIEWER_USERNAME="",
            GNOJO_REVIEWER_PASSWORD_HASH="",
        )
        login_page = self.client.get("/login")
        self.assertIn(b"Reviewer access is not configured", login_page.data)
        self.assertIn(b"disabled", login_page.data)
        self.assertEqual(self.client.get("/curator").status_code, 302)

    def test_policy_keeps_public_detail_get_but_protects_revision_post(self):
        path = "/knowledge/published/example"
        self.assertFalse(ReviewerAccessPolicy.requires_reviewer(path, "GET"))
        self.assertTrue(ReviewerAccessPolicy.requires_reviewer(f"{path}/revise", "POST"))

    def test_policy_covers_bounded_admin_route_groups(self):
        protected = (
            "/content-studio",
            "/content-quality",
            "/curator",
            "/curator/growth",
            "/curator/integrity",
            "/curator/fix/CFX-1",
            "/review",
            "/workflow-studio",
            "/workflow-editor/example.json",
            "/api/workflow-drafts/example.json/lifecycle",
            "/knowledge/builder",
            "/knowledge/drafts",
            "/knowledge/manage/example",
            "/commands/builder",
            "/scripts/builder",
            "/workflow-builder",
        )
        for path in protected:
            with self.subTest(path=path):
                self.assertTrue(ReviewerAccessPolicy.requires_reviewer(path, "GET"))

        for path in ("/", "/workflows", "/wizard", "/knowledge", "/commands", "/scripts", "/search"):
            with self.subTest(public_path=path):
                self.assertFalse(ReviewerAccessPolicy.requires_reviewer(path, "GET"))

    def test_public_history_and_profile_mutations_use_the_shared_csrf_boundary(self):
        protected = (
            ("POST", "/api/device-profiles"),
            ("PATCH", "/api/device-profiles/DEV-1"),
            ("DELETE", "/api/device-profiles/DEV-1"),
            ("POST", "/api/device-profiles/DEV-1/activate"),
            ("POST", "/api/troubleshooting-history/TSH-1/feedback"),
            ("POST", "/troubleshooting-history/TSH-1/delete"),
            ("POST", "/troubleshooting-history/clear"),
        )
        for method, path in protected:
            with self.subTest(method=method, path=path):
                self.assertFalse(ReviewerAccessPolicy.requires_reviewer(path, method))
                self.assertTrue(ReviewerAccessPolicy.requires_csrf(path, method))
                self.assertFalse(ReviewerAccessPolicy.requires_csrf(path, "GET"))

        for method, path in (
            ("POST", "/wizard"),
            ("POST", "/troubleshooting-session/end"),
            ("POST", "/api/workflow-favorites/internet"),
        ):
            with self.subTest(unaffected_path=path):
                self.assertFalse(ReviewerAccessPolicy.requires_csrf(path, method))

    def test_anonymous_public_page_issues_the_shared_csrf_token(self):
        page = self.client.get("/device-profiles")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="gnojo-csrf-token"', page.data)
        self.assertIn(b"/static/js/reviewer_csrf.js", page.data)


if __name__ == "__main__":
    unittest.main()
