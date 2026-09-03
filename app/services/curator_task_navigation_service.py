from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit


@dataclass(frozen=True)
class CuratorTaskNavigation:
    origin: str
    return_url: str
    return_label: str


class CuratorTaskNavigationService:
    """Validate and present explicit task-origin context without storing it."""

    ORIGINS = {
        "knowledge_tasks": "Return to Knowledge Tasks",
        "relationship_proposals": "Return to Relationship Proposals",
        "maintenance": "Return to Fix Wizard",
        "assisted_resolution": "Return to Assisted Resolution",
        "assisted_resolution_batch": "Return to Assisted Resolution Batch",
        "content_quality": "Return to Content Quality",
        "previous_task": "Return to previous task",
    }
    OVERVIEW_QUERY = {
        "status", "include_resolved", "classification", "workflow", "family",
        "rule", "disposition", "q", "sort", "notice",
    }
    RELATIONSHIP_QUERY = {"outcome", "status"}
    MAINTENANCE_QUERY = {"category", "item", "status", "repaired_task", "debt"}
    TASK_QUERY = {"origin", "return_to", "curator_session", "category"}
    PUBLISHED_QUERY = {"q", "category"}

    @classmethod
    def resolve(cls, origin: str, return_url: str, *, task_id: str) -> CuratorTaskNavigation:
        origin = str(origin or "").strip()
        return_url = str(return_url or "").strip()
        if origin not in cls.ORIGINS or not cls._valid(origin, return_url, task_id=task_id):
            return CuratorTaskNavigation("overview", "/curator", "Return to Curator Overview")
        return CuratorTaskNavigation(origin, return_url, cls.ORIGINS[origin])

    @classmethod
    def assisted_task_return(cls, task_id: str, navigation: CuratorTaskNavigation) -> str:
        target = cls.task_return(task_id, navigation)
        return target + "#assisted-resolution"

    @classmethod
    def task_return(cls, task_id: str, navigation: CuratorTaskNavigation, *,
                    session_id: str = "", category: str = "") -> str:
        """Build one validated task URL while retaining its workspace origin."""
        target = f"/curator/tasks/{quote(str(task_id), safe='')}"
        query = {}
        if navigation.origin in cls.ORIGINS:
            query.update(origin=navigation.origin, return_to=navigation.return_url)
        if session_id:
            query["curator_session"] = str(session_id)
        if category and category != "all":
            query["category"] = str(category)
        return target + ("?" + urlencode(query) if query else "")

    @classmethod
    def previous_task_return(cls, task_id: str, navigation: CuratorTaskNavigation, *,
                             session_id: str = "", category: str = "") -> str:
        """Build the single allowed previous-task hop; never nest task hops recursively."""
        if navigation.origin == "previous_task":
            navigation = CuratorTaskNavigation("overview", "/curator", "Return to Curator Overview")
        return cls.task_return(
            task_id, navigation, session_id=session_id, category=category,
        )

    @classmethod
    def valid_assisted_return(cls, value: str) -> str:
        parsed = cls._local(value, allow_encoded_slash=True)
        if not parsed or not parsed.path.startswith("/curator/tasks/"):
            return ""
        task_id = parsed.path.removeprefix("/curator/tasks/")
        if not task_id or "/" in task_id or parsed.fragment != "assisted-resolution":
            return ""
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if set(query) - {"origin", "return_to"}:
            return ""
        nested = cls.resolve(query.get("origin", ""), query.get("return_to", ""), task_id=task_id)
        if query and nested.origin == "overview":
            return ""
        return value

    @classmethod
    def valid_maintenance_return(cls, value: str) -> str:
        return value if cls._valid("maintenance", value, task_id="") else ""

    @classmethod
    def valid_task_return(cls, value: str, *, allow_previous_task: bool = True) -> str:
        """Accept only a bounded task URL whose nested origin contract also validates."""
        parsed = cls._local(value, allow_encoded_slash=True)
        if not parsed or not parsed.path.startswith("/curator/tasks/") or parsed.fragment:
            return ""
        task_id = parsed.path.removeprefix("/curator/tasks/")
        if not task_id or "/" in task_id or "%" in task_id:
            return ""
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len({key for key, _ in pairs}) != len(pairs):
            return ""
        query = dict(pairs)
        if set(query) - cls.TASK_QUERY:
            return ""
        if not query:
            return value
        origin = query.get("origin", "")
        return_to = query.get("return_to", "")
        if not origin or not return_to or (origin == "previous_task" and not allow_previous_task):
            return ""
        nested = cls.resolve(origin, return_to, task_id=task_id)
        return value if nested.origin == origin else ""

    @classmethod
    def valid_published_context(cls, value: str) -> str:
        """Validate a published inventory or one-level article-detail context."""
        parsed = cls._local(value, allow_encoded_slash=True)
        if not parsed or parsed.fragment:
            return ""
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len({key for key, _ in pairs}) != len(pairs):
            return ""
        query = dict(pairs)
        if parsed.path == "/knowledge/published":
            return value if set(query) <= cls.PUBLISHED_QUERY else ""
        article_id = parsed.path.removeprefix("/knowledge/published/")
        if not article_id or "/" in article_id or "%" in article_id or set(query) - {"return_to"}:
            return ""
        nested = query.get("return_to", "")
        return value if (
            not nested
            or cls._valid_published_inventory(nested)
            or cls.valid_task_return(nested)
        ) else ""

    @classmethod
    def _valid_published_inventory(cls, value: str) -> bool:
        parsed = cls._local(value)
        if not parsed or parsed.path != "/knowledge/published" or parsed.fragment:
            return False
        return {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)} <= cls.PUBLISHED_QUERY

    @classmethod
    def _valid(cls, origin: str, value: str, *, task_id: str) -> bool:
        parsed = cls._local(value, allow_encoded_slash=origin == "previous_task")
        if not parsed:
            return False
        query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if origin == "knowledge_tasks":
            return (parsed.path == "/curator" and parsed.fragment == "knowledge-tasks"
                    and query_keys <= cls.OVERVIEW_QUERY)
        if origin == "relationship_proposals":
            return (parsed.path == "/curator/relationship-proposals" and not parsed.fragment
                    and query_keys <= cls.RELATIONSHIP_QUERY)
        if origin == "maintenance":
            session_id = parsed.path.removeprefix("/curator/fix/")
            return (parsed.path.startswith("/curator/fix/") and bool(session_id)
                    and "/" not in session_id and not parsed.fragment
                    and query_keys <= cls.MAINTENANCE_QUERY)
        if origin == "assisted_resolution":
            return (parsed.path == f"/curator/tasks/{task_id}" and not parsed.query
                    and parsed.fragment == "assisted-resolution")
        if origin == "assisted_resolution_batch":
            return (parsed.path == "/curator" and not parsed.query
                    and parsed.fragment == "assisted-resolution-batch")
        if origin == "content_quality":
            return (parsed.path == "/content-quality" and not parsed.query
                    and parsed.fragment in {"", "queueTitle"})
        if origin == "previous_task":
            previous_id = parsed.path.removeprefix("/curator/tasks/")
            return (previous_id != task_id
                    and bool(cls.valid_task_return(value, allow_previous_task=False)))
        return False

    @staticmethod
    def _local(value: str, *, allow_encoded_slash: bool = False):
        if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
            return None
        parsed = urlsplit(value)
        if (parsed.scheme or parsed.netloc or ".." in parsed.path
                or (not allow_encoded_slash and "%2f" in value.casefold())):
            return None
        return parsed
