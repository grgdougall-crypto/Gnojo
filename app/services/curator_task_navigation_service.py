from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


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
        "maintenance": "Return to Maintenance",
        "assisted_resolution": "Return to Assisted Resolution",
        "assisted_resolution_batch": "Return to Assisted Resolution Batch",
    }
    OVERVIEW_QUERY = {
        "status", "include_resolved", "classification", "workflow", "family",
        "rule", "disposition", "q", "sort", "notice",
    }
    RELATIONSHIP_QUERY = {"outcome", "status"}
    MAINTENANCE_QUERY = {"category", "item", "status", "repaired_task", "debt"}

    @classmethod
    def resolve(cls, origin: str, return_url: str, *, task_id: str) -> CuratorTaskNavigation:
        origin = str(origin or "").strip()
        return_url = str(return_url or "").strip()
        if origin not in cls.ORIGINS or not cls._valid(origin, return_url, task_id=task_id):
            return CuratorTaskNavigation("overview", "/curator", "Return to Curator Overview")
        return CuratorTaskNavigation(origin, return_url, cls.ORIGINS[origin])

    @classmethod
    def assisted_task_return(cls, task_id: str, navigation: CuratorTaskNavigation) -> str:
        from urllib.parse import quote

        target = f"/curator/tasks/{quote(task_id, safe='')}"
        if navigation.origin in cls.ORIGINS:
            target += (
                f"?origin={quote(navigation.origin, safe='')}"
                f"&return_to={quote(navigation.return_url, safe='')}"
            )
        return target + "#assisted-resolution"

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
    def _valid(cls, origin: str, value: str, *, task_id: str) -> bool:
        parsed = cls._local(value)
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
