"""Minimal environment-backed Reviewer/Admin authentication for Gnojo."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from flask import current_app, request, session
from werkzeug.security import check_password_hash


@dataclass(frozen=True)
class ReviewerIdentity:
    username: str
    role: str = "reviewer_admin"


class AuthenticationService:
    """Authenticate the single configured reviewer and manage its signed session."""

    AUTHENTICATED_KEY = "reviewer_authenticated"
    USERNAME_KEY = "reviewer_username"
    ROLE_KEY = "reviewer_role"
    CSRF_KEY = "reviewer_csrf_token"

    @staticmethod
    def configured_identity() -> ReviewerIdentity | None:
        username = str(current_app.config.get("GNOJO_REVIEWER_USERNAME") or "").strip()
        password_hash = str(
            current_app.config.get("GNOJO_REVIEWER_PASSWORD_HASH") or ""
        ).strip()
        if (
            not username
            or not password_hash
            or not current_app.config.get("GNOJO_STABLE_SESSION_SECRET_CONFIGURED")
        ):
            return None
        return ReviewerIdentity(username=username)

    @classmethod
    def authenticate(cls, username: str, password: str) -> ReviewerIdentity | None:
        identity = cls.configured_identity()
        password_hash = str(
            current_app.config.get("GNOJO_REVIEWER_PASSWORD_HASH") or ""
        ).strip()
        if identity is None:
            return None

        username_matches = hmac.compare_digest(str(username or ""), identity.username)
        try:
            password_matches = check_password_hash(password_hash, str(password or ""))
        except (TypeError, ValueError):
            password_matches = False
        return identity if username_matches and password_matches else None

    @classmethod
    def sign_in(cls, identity: ReviewerIdentity) -> None:
        session.clear()
        session.permanent = True
        session[cls.AUTHENTICATED_KEY] = True
        session[cls.USERNAME_KEY] = identity.username
        session[cls.ROLE_KEY] = identity.role
        session[cls.CSRF_KEY] = secrets.token_urlsafe(32)

    @classmethod
    def sign_out(cls) -> None:
        session.clear()

    @classmethod
    def current_identity(cls) -> ReviewerIdentity | None:
        if session.get(cls.AUTHENTICATED_KEY) is not True:
            return None
        username = str(session.get(cls.USERNAME_KEY) or "").strip()
        role = str(session.get(cls.ROLE_KEY) or "").strip()
        configured = cls.configured_identity()
        if (
            configured is None
            or role != configured.role
            or not hmac.compare_digest(username, configured.username)
        ):
            return None
        return configured

    @classmethod
    def is_authenticated(cls) -> bool:
        return cls.current_identity() is not None

    @classmethod
    def csrf_token(cls) -> str:
        token = str(session.get(cls.CSRF_KEY) or "")
        if not token:
            token = secrets.token_urlsafe(32)
            session[cls.CSRF_KEY] = token
        return token

    @classmethod
    def valid_csrf(cls) -> bool:
        supplied = str(
            request.form.get("authenticity_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        expected = str(session.get(cls.CSRF_KEY) or "")
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))


class ReviewerAccessPolicy:
    """Explicitly identify the current bounded Reviewer/Admin route surface."""

    PROTECTED_PREFIXES = (
        "/curator",
        "/review",
        "/content-quality",
        "/workflow-studio",
        "/workflow-editor",
        "/api/workflow-drafts",
        "/knowledge/builder",
        "/knowledge/drafts",
        "/knowledge/manage",
        "/api/knowledge/drafts",
        "/commands/builder",
        "/scripts/builder",
        "/workflow-builder",
        "/__dev/phase3-relationship-harness",
    )
    PROTECTED_EXACT_PATHS = frozenset({
        "/content-studio",
        "/knowledge/articles/current",
    })
    PUBLIC_CSRF_EXACT_PATHS = frozenset({
        "/troubleshooting-history/clear",
    })

    @classmethod
    def requires_reviewer(cls, path: str, method: str) -> bool:
        normalized_path = str(path or "")
        if normalized_path in cls.PROTECTED_EXACT_PATHS:
            return True
        if any(
            normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
            for prefix in cls.PROTECTED_PREFIXES
        ):
            return True
        return bool(
            method.upper() == "POST"
            and normalized_path.startswith("/knowledge/published/")
            and normalized_path.endswith("/revise")
        )

    @classmethod
    def requires_csrf(cls, path: str, method: str) -> bool:
        """Identify state-changing routes that require the shared session token."""
        normalized_path = str(path or "")
        normalized_method = str(method or "").upper()
        if normalized_method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        if cls.requires_reviewer(normalized_path, normalized_method):
            return True
        if normalized_path in cls.PUBLIC_CSRF_EXACT_PATHS:
            return True
        if normalized_path == "/api/device-profiles":
            return normalized_method == "POST"
        if normalized_path.startswith("/api/device-profiles/"):
            return normalized_method in {"POST", "PATCH", "DELETE"}
        if normalized_path.startswith("/api/troubleshooting-history/"):
            return normalized_method == "POST" and normalized_path.endswith("/feedback")
        return bool(
            normalized_method == "POST"
            and normalized_path.startswith("/troubleshooting-history/")
            and normalized_path.endswith("/delete")
        )


def safe_login_return(value: object, fallback: str) -> str:
    """Accept only absolute-path local destinations for post-login navigation."""
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        decoded_path = unquote(parsed.path)
    except (UnicodeError, ValueError):
        return fallback
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        or parsed.scheme
        or parsed.netloc
        or parsed.path in {"/login", "/logout"}
    ):
        return fallback
    return candidate
