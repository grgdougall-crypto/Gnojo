import json
import os
import re
import secrets
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from flask import (
    Flask,
    abort,
    g,
    Response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from dataclasses import asdict

from markupsafe import Markup, escape
from dotenv import load_dotenv

from app.engine.decision_engine import DecisionEngine
from app.knowledge.knowledge_base import KnowledgeBase
from app.repositories.knowledge_repository import (
    ArticleAlreadyExistsError,
    ArticleNotFoundError,
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from app.repositories.command_repository import CommandRepository
from app.repositories.script_repository import ScriptRepository

from app.services.search_service import SearchService
from app.services.article_tag_service import ArticleTagService

from app.services.relationship_service import RelationshipService

from app.services.explanation_service import ExplanationService

from app.services.draft_generation_service import DraftGenerationService

from app.services.publish_validation_service import (
    PublishValidationService,
)

from app.services.publication_service import (
    PublicationService,
)

from app.engine.workflow_generation_engine import (
    WorkflowGenerationEngine,
)

from app.services.workflow_validation_service import (
    WorkflowValidationService,
)

from app.services.workflow_draft_service import (
    WorkflowDraftError,
    WorkflowDraftService,
)

from app.services.workflow_outline_service import (
    WorkflowOutlineService,
)

from app.services.workflow_statistics_service import (
    WorkflowStatisticsService,
)

from app.services.workflow_node_service import (
    WorkflowNodeService,
)

from app.services.workflow_publication_service import (
    WorkflowPublicationError,
    WorkflowPublicationService,
)

from app.services.workflow_ai_service import (
    WorkflowAIError,
    WorkflowAIService,
)
from app.services.workflow_export_service import WorkflowExportError, WorkflowExportService
from app.services.workflow_metadata_service import workflow_category, workflow_platform
from app.services.device_profile_service import DeviceProfileError, DeviceProfileService
from app.services.workflow_condition_service import WorkflowConditionError, resolve_applicable_node
from app.services.learning_mode_service import LearningModeService
from app.services.troubleshooting_history_service import (
    TroubleshootingHistoryError,
    TroubleshootingHistoryService,
)
from app.services.content_quality_service import ContentQualityService
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_task_service import CuratorTaskService
from curator.locking import AuditAlreadyRunningError
from curator.memory import CuratorMemoryError
from app.services.workflow_coverage_service import (
    WorkflowCoverageError,
    WorkflowCoverageService,
)
from app.services.workflow_help_text_service import (
    WorkflowHelpTextError,
    WorkflowHelpTextService,
)
from app.services.article_review_service import ArticleReviewError, ArticleReviewService
from app.services.article_source_finder_service import ArticleSourceFinderError, ArticleSourceFinderService
from app.services.script_authoring_service import ScriptAuthoringError, ScriptAuthoringService

load_dotenv()

app = Flask(__name__)

@app.template_filter("highlight")
def highlight_search_term(value, query):
    """
    Safely highlight a search term within displayed text.
    """

    if value is None:
        return ""

    safe_value = escape(str(value))
    normalized_query = str(query).strip()

    if not normalized_query:
        return safe_value

    pattern = re.compile(
        re.escape(normalized_query),
        re.IGNORECASE,
    )

    highlighted_value = pattern.sub(
        lambda match: (
            f"<mark>{match.group(0)}</mark>"
        ),
        str(safe_value),
    )

    return Markup(highlighted_value)


@app.template_filter("friendly_datetime")
def friendly_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%b %d, %Y · %I:%M %p")
    except (TypeError, ValueError):
        return "Unknown time"


@app.template_filter("friendly_node")
def friendly_node(value):
    label = re.sub(r"^(q|instr|res|transition)_", "", str(value or ""))
    return label.replace("_", " ").strip().title() or "Unknown step"

knowledge_repository = KnowledgeRepository()
command_repository = CommandRepository()
script_repository = ScriptRepository()
script_authoring_service = ScriptAuthoringService()
search_service = SearchService()
relationship_service = RelationshipService()
explanation_service = ExplanationService()
draft_generation_service = DraftGenerationService()
publish_validation_service = PublishValidationService()
publication_service = PublicationService()


app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)


@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID", "").strip()[:64] or uuid4().hex[:12]


@app.after_request
def attach_request_id(response):
    response.headers["X-Request-ID"] = getattr(g, "request_id", "unknown")
    return response


def error_response(status, title, message):
    request_id = getattr(g, "request_id", "unknown")
    if request.path.startswith("/api/"):
        return {"ok": False, "error": message, "request_id": request_id}, status
    return render_template(
        "error.html",
        status=status,
        title=title,
        message=message,
        request_id=request_id,
    ), status


@app.errorhandler(404)
def page_not_found(error):
    return error_response(404, "We couldn't find that page", "The link may be outdated, or the workflow or resource may no longer be available.")


@app.errorhandler(500)
def internal_error(error):
    app.logger.error(json.dumps({
        "event": "unhandled_error",
        "request_id": getattr(g, "request_id", "unknown"),
        "method": request.method,
        "path": request.path,
        "error_type": type(error).__name__,
    }), exc_info=error)
    return error_response(500, "Gnojo hit an unexpected problem", "Your data was not intentionally changed. Return home and try again, or use the request ID below when reporting the problem.")


@app.errorhandler(WorkflowDraftError)
@app.errorhandler(WorkflowPublicationError)
@app.errorhandler(DeviceProfileError)
@app.errorhandler(TroubleshootingHistoryError)
@app.errorhandler(WorkflowCoverageError)
@app.errorhandler(ArticleReviewError)
def saved_data_error(error):
    app.logger.warning(json.dumps({
        "event": "saved_data_error",
        "request_id": getattr(g, "request_id", "unknown"),
        "path": request.path,
        "error_type": type(error).__name__,
    }))
    return error_response(409, "Saved data needs attention", str(error))

current_draft = None

AVAILABLE_WORKFLOWS = {
    "internet": {
        "name": "Internet Connection",
        "description": "Troubleshoot Wi-Fi, Ethernet, routers, and connectivity.",
        "icon": "bi-wifi",
        "category": "Networking",
        "platform": "Cross-platform",
    },
    "printer": {
        "name": "Printer",
        "description": (
            "Troubleshoot power, connections, print queues, and paper issues."
        ),
        "icon": "bi-printer",
        "category": "Desktop Support",
        "platform": "Cross-platform",
    },
    "network_diagnostics": {
        "name": "Advanced Network Diagnostics",
        "description": "Perform advanced network diagnostics to identify and resolve connectivity issues.",
        "icon": "bi-wifi",
        "category": "Networking",
        "platform": "Cross-platform",
    },
    "windows_slow": {
        "name": "Computer Running Slowly",
        "description": "Diagnose Windows performance, memory, storage, startup, updates, and security issues.",
        "icon": "bi-speedometer2",
        "category": "Desktop Support",
        "platform": "Windows",
    },
    "application_crash": {
        "name": "Application Keeps Crashing",
        "description": "Safely diagnose repeated application freezes, crashes, and startup failures.",
        "icon": "bi-app-indicator",
        "category": "Desktop Support",
        "platform": "Windows",
    },
    "no_sound": {
        "name": "No Sound",
        "description": "Restore Windows audio by checking output, volume, connections, and built-in diagnostics.",
        "icon": "bi-volume-mute",
        "category": "Desktop Support",
        "platform": "Windows",
    },
    "low_storage": {
        "name": "Low Disk Space",
        "description": "Find and safely reclaim storage without deleting Windows system files.",
        "icon": "bi-device-hdd",
        "category": "Desktop Support",
        "platform": "Windows",
    }
}


def available_workflows():
    """Build the runtime catalog from built-ins and active publications."""
    catalog = {
        workflow_id: {**details, "source": "built_in"}
        for workflow_id, details in AVAILABLE_WORKFLOWS.items()
    }
    try:
        snapshots = WorkflowPublicationService().list_current()
    except (OSError, WorkflowPublicationError):
        snapshots = []
    for snapshot in snapshots:
        workflow = snapshot["workflow"]
        workflow_id = workflow.get("workflow_id")
        if not workflow_id:
            continue
        catalog[workflow_id] = {
            "name": workflow.get("name") or workflow_id.replace("_", " ").title(),
            "description": workflow.get("description") or "Follow this guided troubleshooting workflow.",
            "icon": workflow.get("icon") or "bi-signpost-split",
            "source": "published",
            "version": snapshot.get("publication", {}).get("version"),
            "category": workflow_category(workflow),
            "platform": workflow_platform(workflow),
        }
    return catalog


def load_runtime_workflow(engine, workflow_id, catalog=None, version=None):
    catalog = catalog or available_workflows()
    details = catalog.get(workflow_id)
    if details is None:
        raise FileNotFoundError(workflow_id)
    if details.get("source") == "published":
        snapshot = WorkflowPublicationService().load_version(workflow_id, version) if version else WorkflowPublicationService().load_current(workflow_id)
        if not snapshot:
            raise FileNotFoundError(workflow_id)
        engine.load_workflow_data(snapshot["workflow"])
    else:
        engine.load_workflow(workflow_id)


def active_device_profile():
    profile_id = session.get("active_device_profile_id")
    if not profile_id:
        return None
    try:
        profile = DeviceProfileService().get(profile_id)
    except DeviceProfileError:
        profile = None
    if profile is None:
        session.pop("active_device_profile_id", None)
    return profile


def workflow_device_compatibility(workflow_info, device):
    if not device:
        return "neutral"
    workflow_os = str(workflow_info.get("platform") or "Cross-platform").lower()
    device_os = str(device.get("platform") or "Other").lower()
    if workflow_os == "cross-platform" or device_os == "other":
        return "compatible"
    return "recommended" if workflow_os == device_os else "incompatible"


def favorite_workflow_ids(catalog=None):
    catalog = catalog or available_workflows()
    saved = session.get("favorite_workflow_ids", [])
    if not isinstance(saved, list):
        saved = []
    valid = []
    for workflow_id in saved:
        if isinstance(workflow_id, str) and workflow_id in catalog and workflow_id not in valid:
            valid.append(workflow_id)
    if valid != saved:
        session["favorite_workflow_ids"] = valid
    return valid


def recent_workflow_ids(catalog, limit=10):
    recent = []
    try:
        for record in TroubleshootingHistoryService().list(50):
            workflow_id = record.get("workflow_id")
            if workflow_id in catalog and workflow_id not in recent:
                recent.append(workflow_id)
            if len(recent) >= limit:
                break
    except (OSError, TroubleshootingHistoryError):
        pass
    return recent


def active_troubleshooting_session(catalog=None):
    workflow_id = session.get("workflow")
    node_id = session.get("current_node")
    if not workflow_id or not node_id or session.get("workflow_complete"):
        return None
    catalog = catalog or available_workflows()
    workflow = catalog.get(workflow_id)
    if not workflow:
        clear_troubleshooting_session()
        return None
    return {
        "workflow_id": workflow_id,
        "workflow_name": workflow["name"],
        "node_id": node_id,
        "step": session.get("step", 1),
        "version": session.get("workflow_version"),
        "learning_mode": session.get("learning_mode", False),
    }


def clear_troubleshooting_session():
    abandon_active_history()
    for key in ("workflow", "workflow_version", "workflow_complete", "current_node", "node_history", "step", "skipped_nodes", "learning_concepts"):
        session.pop(key, None)
    session.pop("troubleshooting_history_id", None)


def abandon_active_history():
    history_id = session.get("troubleshooting_history_id")
    if history_id:
        try:
            TroubleshootingHistoryService().abandon(history_id)
        except (OSError, TroubleshootingHistoryError):
            app.logger.warning("Unable to mark troubleshooting history as abandoned.")


def track_history_progress(node_id, action="advance", workflow_id=None,
                           workflow_name=None, version=None):
    history_id = session.get("troubleshooting_history_id")
    if not history_id:
        return
    try:
        TroubleshootingHistoryService().progress(
            history_id,
            node_id,
            action=action,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            version=version,
        )
    except (OSError, TroubleshootingHistoryError):
        app.logger.warning("Unable to update troubleshooting history.")


@app.route("/")
def home():
    if request.args.get("learning") in {"0", "1"}:
        session["learning_mode"] = request.args.get("learning") == "1"
        if not session["learning_mode"]:
            session.pop("learning_concepts", None)
    active_device = active_device_profile()
    workflows = available_workflows()
    for details in workflows.values():
        details["compatibility"] = workflow_device_compatibility(details, active_device)
    priority = {"recommended": 0, "compatible": 1, "neutral": 2, "incompatible": 3}
    workflows = dict(sorted(workflows.items(), key=lambda item: (priority[item[1]["compatibility"]], item[1]["name"].lower())))

    favorite_ids = favorite_workflow_ids(workflows)
    recent_ids = recent_workflow_ids(workflows)
    featured_ids = []
    for workflow_id in favorite_ids + recent_ids:
        if workflow_id not in featured_ids:
            featured_ids.append(workflow_id)
    for workflow_id, details in workflows.items():
        if details["compatibility"] == "recommended" and workflow_id not in featured_ids:
            featured_ids.append(workflow_id)
    for workflow_id in workflows:
        if workflow_id not in featured_ids:
            featured_ids.append(workflow_id)
    featured_workflows = {
        workflow_id: workflows[workflow_id]
        for workflow_id in featured_ids[:4]
    }

    return render_template(
        "index.html",
        workflows=workflows,
        featured_workflows=featured_workflows,
        workflow_count=len(workflows),
        favorite_ids=favorite_ids,
        active_device=active_device,
        learning_mode=session.get("learning_mode", False),
        active_session=active_troubleshooting_session(workflows),
    )


@app.route("/workflows")
def workflow_catalog():
    device = active_device_profile()
    workflows = available_workflows()
    for details in workflows.values():
        details["compatibility"] = workflow_device_compatibility(details, device)
    workflows = dict(sorted(workflows.items(), key=lambda item: (
        item[1]["category"].lower(), item[1]["name"].lower()
    )))
    return render_template(
        "workflow_catalog.html",
        workflows=workflows,
        active_device=device,
        favorite_ids=favorite_workflow_ids(workflows),
        recent_ids=recent_workflow_ids(workflows),
    )


@app.route("/api/workflow-favorites/<workflow_id>", methods=["POST"])
def toggle_workflow_favorite(workflow_id):
    catalog = available_workflows()
    if workflow_id not in catalog:
        return {"ok": False, "error": "Workflow not found."}, 404
    favorites = favorite_workflow_ids(catalog)
    favorite = workflow_id not in favorites
    if favorite:
        favorites.append(workflow_id)
    else:
        favorites.remove(workflow_id)
    session["favorite_workflow_ids"] = favorites
    return {"ok": True, "workflow_id": workflow_id, "favorite": favorite}


@app.route("/troubleshooting-session/end", methods=["POST"])
def end_troubleshooting_session():
    clear_troubleshooting_session()
    return redirect(url_for("home", session_ended="1", _anchor="workflows"))


@app.route("/device-profiles")
def device_profiles():
    return render_template(
        "device_profiles.html",
        profiles=DeviceProfileService().list(),
        active_device=active_device_profile(),
    )


@app.route("/troubleshooting-history")
def troubleshooting_history():
    service = TroubleshootingHistoryService()
    records = service.list(200)
    return render_template(
        "troubleshooting_history.html",
        records=records,
        analytics=service.analytics(records),
    )


@app.route("/troubleshooting-history/<history_id>")
def troubleshooting_history_detail(history_id):
    record = TroubleshootingHistoryService().get(history_id)
    if record is None:
        abort(404)
    return render_template("troubleshooting_history_detail.html", record=record)


@app.route("/troubleshooting-history/<history_id>/delete", methods=["POST"])
def delete_troubleshooting_history(history_id):
    try:
        TroubleshootingHistoryService().delete(history_id)
    except FileNotFoundError:
        abort(404)
    if session.get("troubleshooting_history_id") == history_id:
        clear_troubleshooting_session()
    return redirect(url_for("troubleshooting_history"))


@app.route("/api/troubleshooting-history/<history_id>/feedback", methods=["POST"])
def submit_troubleshooting_feedback(history_id):
    try:
        feedback = TroubleshootingHistoryService().add_feedback(
            history_id, request.get_json(silent=True)
        )
    except FileNotFoundError:
        return {"ok": False, "error": "Troubleshooting session not found."}, 404
    except TroubleshootingHistoryError as error:
        return {"ok": False, "error": str(error)}, 400
    return {"ok": True, "feedback": feedback}


@app.route("/troubleshooting-history/clear", methods=["POST"])
def clear_troubleshooting_history():
    TroubleshootingHistoryService().clear()
    clear_troubleshooting_session()
    return redirect(url_for("troubleshooting_history"))


@app.route("/api/device-profiles", methods=["POST"])
def create_device_profile():
    try:
        profile = DeviceProfileService().create(request.get_json(silent=True))
    except DeviceProfileError as error:
        return {"ok": False, "error": str(error)}, 400
    if request.get_json(silent=True).get("activate", True):
        session["active_device_profile_id"] = profile["id"]
    return {"ok": True, "profile": profile}, 201


@app.route("/api/device-profiles/<profile_id>", methods=["PATCH", "DELETE"])
def update_or_delete_device_profile(profile_id):
    service = DeviceProfileService()
    try:
        if request.method == "DELETE":
            service.delete(profile_id)
            if session.get("active_device_profile_id") == profile_id:
                session.pop("active_device_profile_id", None)
            return {"ok": True}
        profile = service.update(profile_id, request.get_json(silent=True))
        return {"ok": True, "profile": profile}
    except FileNotFoundError:
        return {"ok": False, "error": "Device profile not found."}, 404
    except DeviceProfileError as error:
        return {"ok": False, "error": str(error)}, 400


@app.route("/api/device-profiles/<profile_id>/activate", methods=["POST"])
def activate_device_profile(profile_id):
    try:
        profile = DeviceProfileService().get(profile_id)
    except DeviceProfileError as error:
        return {"ok": False, "error": str(error)}, 400
    if profile is None:
        return {"ok": False, "error": "Device profile not found."}, 404
    session["active_device_profile_id"] = profile_id
    return {"ok": True, "profile": profile}

@app.route("/content-studio")
def content_studio():

    return render_template(
        "content_studio.html",
    )


@app.route("/content-quality")
def content_quality():
    catalog = available_workflows()
    workflow_data = {}
    for workflow_id in catalog:
        engine = DecisionEngine()
        try:
            load_runtime_workflow(engine, workflow_id, catalog, catalog[workflow_id].get("version"))
        except (FileNotFoundError, WorkflowPublicationError, ValueError):
            continue
        workflow_data[workflow_id] = engine.workflow
    drafts = {
        item["workflow_id"]: item["filename"]
        for item in WorkflowDraftService().list_drafts()
        if item.get("workflow_id") and not item.get("is_damaged")
    }
    records = TroubleshootingHistoryService().list(500)
    report = ContentQualityService().build(workflow_data, records, drafts)
    return render_template("content_quality.html", report=report)


@app.route("/curator")
def curator_dashboard():
    status = request.args.get("status", "")
    messages = {
        "completed": ("success", "Curator audit completed. The dashboard now shows the latest operational findings."),
        "running": ("warning", "A Curator audit is already running. Return shortly to review the completed report."),
        "failed": ("danger", "The Curator audit could not be completed. Existing reports and trusted content were not changed."),
    }
    kind, message = messages.get(status, ("info", ""))
    try:
        dashboard = CuratorDashboardService().dashboard(sort_by=request.args.get("sort", "debt"))
    except CuratorMemoryError:
        dashboard = {"has_audit": False, "tasks": [], "recent_audits": []}
        kind, message = "danger", "Curator memory could not be read. Existing trusted content was not changed."
    return render_template("curator_dashboard.html", dashboard=dashboard, status_kind=kind, status_message=message)


@app.route("/curator/tasks/<task_id>")
def curator_task_detail(task_id):
    try:
        task = CuratorTaskService().get(task_id)
    except CuratorMemoryError:
        abort(404)
    messages = {
        "updated": ("success", "Knowledge Task updated."),
        "invalid": ("danger", "The requested task change could not be applied."),
    }
    kind, message = messages.get(request.args.get("status", ""), ("info", ""))
    return render_template(
        "curator_task_detail.html", task=task,
        owners=CuratorTaskService.OWNERS, priorities=CuratorTaskService.PRIORITIES,
        status_kind=kind, status_message=message,
    )


@app.post("/curator/tasks/<task_id>/actions")
def curator_task_action(task_id):
    try:
        CuratorTaskService().update(
            task_id,
            action=request.form.get("action", ""),
            owner=request.form.get("owner", ""),
            priority=request.form.get("priority", ""),
            note=request.form.get("note", ""),
        )
        status = "updated"
    except CuratorMemoryError:
        status = "invalid"
    return redirect(url_for("curator_task_detail", task_id=task_id, status=status))


@app.route("/curator/tasks/<task_id>/repair-preview")
def curator_task_repair_preview(task_id):
    try:
        task = CuratorTaskService().get(task_id)
    except CuratorMemoryError:
        abort(404)
    return render_template("curator_repair_preview.html", task=task)


@app.route("/curator/run", methods=["POST"])
def run_curator_audit():
    try:
        CuratorDashboardService().run_audit()
        status = "completed"
    except AuditAlreadyRunningError:
        status = "running"
    except Exception as error:
        app.logger.error(json.dumps({"event": "curator_audit_failed", "request_id": g.request_id, "error_type": type(error).__name__}))
        status = "failed"
    return redirect(url_for("curator_dashboard", status=status))

@app.route("/workflow-studio")
def workflow_studio():

    draft_service = WorkflowDraftService()
    drafts = draft_service.list_drafts()
    draft_by_workflow = {
        item["workflow_id"]: item
        for item in drafts
        if item.get("workflow_id") and not item.get("is_damaged")
    }
    built_ins = []
    for workflow_id, details in AVAILABLE_WORKFLOWS.items():
        engine = DecisionEngine()
        try:
            engine.load_workflow(workflow_id)
        except (OSError, ValueError):
            continue
        workflow = engine.workflow
        existing = draft_by_workflow.get(workflow_id)
        built_ins.append({
            "workflow_id": workflow_id,
            "name": workflow.get("name") or details["name"],
            "description": workflow.get("description") or details["description"],
            "category": workflow_category(workflow),
            "platform": workflow_platform(workflow),
            "estimated_steps": workflow.get("estimated_steps"),
            "draft_filename": existing.get("filename") if existing else None,
        })

    return render_template(
        "workflow_studio.html",
        drafts=drafts,
        built_ins=built_ins,
    )


@app.route("/workflow-studio/built-ins/<workflow_id>/copy", methods=["POST"])
def copy_builtin_workflow(workflow_id):
    if workflow_id not in AVAILABLE_WORKFLOWS:
        abort(404)

    draft_service = WorkflowDraftService()
    existing = next(
        (
            item for item in draft_service.list_drafts()
            if item.get("workflow_id") == workflow_id and not item.get("is_damaged")
        ),
        None,
    )
    if existing:
        return redirect(url_for("workflow_editor", filename=existing["filename"]))

    engine = DecisionEngine()
    engine.load_workflow(workflow_id)
    workflow = deepcopy(engine.workflow)
    workflow["status"] = "Editable Copy"
    workflow["draft_origin"] = {
        "type": "built_in",
        "workflow_id": workflow_id,
    }
    validation = WorkflowValidationService().validate(workflow)
    if not validation["is_valid"]:
        return error_response(
            400,
            "This workflow cannot be copied yet",
            "The built-in workflow must pass validation before an editable copy can be created.",
        )
    filename = draft_service.save_draft(workflow)
    return redirect(url_for("workflow_editor", filename=filename))

@app.route("/workflow-editor/<filename>")
def workflow_editor(filename):

    draft_service = WorkflowDraftService()

    workflow = draft_service.get_draft(
        filename
    )

    if workflow is None:
        abort(404)

    statistics = (
        WorkflowStatisticsService()
        .build(workflow)
    )

    nodes = (
        WorkflowNodeService()
        .build(workflow)
    )

    return render_template(
        "workflow_editor.html",
        workflow=workflow,
        statistics=statistics,
        nodes=nodes,
        filename=filename,
        workflow_category=workflow_category(workflow),
        workflow_platform=workflow_platform(workflow),
    )


@app.route(
    "/api/workflow-drafts/<filename>/nodes/<node_id>",
    methods=["PATCH"],
)
def update_workflow_node(filename, node_id):
    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return {"ok": False, "error": "A JSON object is required."}, 400

    try:
        workflow = WorkflowDraftService().update_node(
            filename,
            node_id,
            payload,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "Workflow draft not found."}, 404
    except KeyError:
        return {"ok": False, "error": "Workflow node not found."}, 404
    except (WorkflowDraftError, ValueError) as error:
        return {"ok": False, "error": str(error)}, 400

    node = next(
        (
            item
            for item in WorkflowNodeService().build(workflow)
            if item["id"] == node_id
        ),
        None,
    )

    return {"ok": True, "node": node}


@app.route(
    "/api/workflow-drafts/<filename>/settings",
    methods=["PATCH"],
)
def update_workflow_settings(filename):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "A JSON object is required."}, 400

    try:
        workflow = WorkflowDraftService().update_settings(filename, payload)
    except FileNotFoundError:
        return {"ok": False, "error": "Workflow draft not found."}, 404
    except (WorkflowDraftError, ValueError) as error:
        return {"ok": False, "error": str(error)}, 400

    statistics = WorkflowStatisticsService().build(workflow)
    return {
        "ok": True,
        "settings": {
            "workflow_id": workflow.get("workflow_id"),
            "name": workflow.get("name"),
            "description": workflow.get("description", ""),
            "category": workflow.get("category", "Uncategorized"),
            "platform": workflow.get("platform", "Cross-platform"),
            "estimated_steps": workflow.get("estimated_steps"),
            "start_node": workflow.get("start_node"),
            "start_node_title": statistics.get("start_node_title"),
        },
    }


@app.route(
    "/api/workflow-drafts/<filename>/nodes/<node_id>/improve",
    methods=["POST"],
)
def improve_workflow_node(filename, node_id):
    workflow = WorkflowDraftService().get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    nodes = workflow.get("nodes")
    if not isinstance(nodes, dict) or node_id not in nodes:
        return {"ok": False, "error": "Workflow node not found."}, 404

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "A JSON object is required."}, 400

    try:
        suggestion = WorkflowAIService().improve_node(
            node_id,
            nodes[node_id],
            payload.get("style"),
        )
    except WorkflowAIError as error:
        return {"ok": False, "error": str(error)}, 400

    return {"ok": True, **suggestion}


@app.route(
    "/api/workflow-drafts/<filename>/nodes/<node_id>/coverage/help-text",
    methods=["POST"],
)
def generate_workflow_help_text(filename, node_id):
    draft_service = WorkflowDraftService()
    workflow = draft_service.get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404
    node = workflow.get("nodes", {}).get(node_id)
    if not isinstance(node, dict):
        return {"ok": False, "error": "This draft changed after the page opened. Refresh the Workflow Designer and select the node again."}, 409
    payload = request.get_json(silent=True) or {}
    service = WorkflowHelpTextService()
    try:
        if payload.get("action") == "accept":
            help_text = service.validate_candidate(node, payload.get("help_text"))
            workflow = draft_service.update_node(
                filename, node_id, {"help_text": help_text}
            )
            normalized = next(
                item for item in WorkflowNodeService().build(workflow)
                if item["id"] == node_id
            )
            return {
                "ok": True,
                "accepted": True,
                "help_text": help_text,
                "node": normalized,
            }

        suggestion = service.suggest(workflow, node_id, node)
    except (WorkflowHelpTextError, WorkflowDraftError, ValueError) as error:
        return {"ok": False, "error": str(error)}, 400
    return {"ok": True, "accepted": False, **suggestion}


@app.route(
    "/api/workflow-drafts/<filename>/nodes/<node_id>/coverage/article",
    methods=["POST"],
)
def create_workflow_article_draft(filename, node_id):
    draft_service = WorkflowDraftService()
    workflow = draft_service.get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404
    node = workflow.get("nodes", {}).get(node_id)
    if not isinstance(node, dict):
        return {"ok": False, "error": "This draft changed after the page opened. Refresh the Workflow Designer and select the node again."}, 409
    try:
        article = WorkflowCoverageService().create_article_draft(workflow, node_id, node)
        article["workflow_origin"] = {
            "filename": filename,
            "workflow_id": workflow.get("workflow_id"),
            "workflow_name": workflow.get("name"),
            "node_id": node_id,
            "node_title": node.get("title") or node_id.replace("_", " ").title(),
        }
        created = True
        try:
            knowledge_repository.save_draft(article)
        except ArticleAlreadyExistsError:
            article = knowledge_repository.get_draft(article["id"])
            if not isinstance(article.get("workflow_origin"), dict):
                article["workflow_origin"] = {
                    "filename": filename,
                    "workflow_id": workflow.get("workflow_id"),
                    "workflow_name": workflow.get("name"),
                    "node_id": node_id,
                    "node_title": node.get("title") or node_id.replace("_", " ").title(),
                }
                knowledge_repository.save_draft(article, overwrite=True)
            created = False
        workflow = draft_service.update_node(
            filename, node_id, {"knowledge_article": article["id"]}
        )
    except (WorkflowCoverageError, WorkflowDraftError, KnowledgeRepositoryError, ValueError) as error:
        return {"ok": False, "error": str(error)}, 400
    normalized = next(
        item for item in WorkflowNodeService().build(workflow) if item["id"] == node_id
    )
    return {
        "ok": True,
        "created": created,
        "article_id": article["id"],
        "review_url": url_for("review_draft", article_id=article["id"]),
        "node": normalized,
    }, 201 if created else 200


@app.route("/api/workflow-drafts/<filename>/validation")
def validate_workflow_draft(filename):
    workflow = WorkflowDraftService().get_draft(filename)

    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    validation = WorkflowValidationService().validate(workflow)
    issues = []

    for level, messages in (
        ("error", validation["errors"]),
        ("warning", validation["warnings"]),
    ):
        for message in messages:
            match = re.search(r"node '([^']+)'", message, re.IGNORECASE)
            issues.append(
                {
                    "level": level,
                    "message": message,
                    "node_id": match.group(1) if match else None,
                }
            )

    return {
        "ok": True,
        "is_valid": validation["is_valid"],
        "error_count": len(validation["errors"]),
        "warning_count": len(validation["warnings"]),
        "issues": issues,
        "reachable_count": len(validation["reachable_nodes"]),
        "unreachable_count": len(validation["unreachable_nodes"]),
    }


@app.route("/api/workflow-drafts/<filename>/export/<export_format>")
def export_workflow_draft(filename, export_format):
    workflow = WorkflowDraftService().get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    try:
        content, download_name, mimetype = WorkflowExportService().export(workflow, export_format)
    except WorkflowExportError as error:
        return {"ok": False, "error": str(error)}, 400

    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.route("/api/workflow-drafts/<filename>/publication")
def workflow_publication_status(filename):
    workflow = WorkflowDraftService().get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    try:
        publication_service = WorkflowPublicationService()
        status = publication_service.status(workflow.get("workflow_id"))
    except WorkflowPublicationError as error:
        return {"ok": False, "error": str(error)}, 400

    latest_hash = status["versions"][0]["content_hash"] if status["versions"] else None
    return {
        "ok": True,
        **status,
        "has_unpublished_changes": latest_hash != publication_service.content_hash(workflow),
    }


@app.route(
    "/api/workflow-drafts/<filename>/publication",
    methods=["POST"],
)
def publish_workflow_draft(filename):
    workflow = WorkflowDraftService().get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "A JSON object is required."}, 400

    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        return {"ok": False, "error": "Version label must be text."}, 400

    try:
        publication_service = WorkflowPublicationService()
        status = publication_service.publish(
            workflow,
            source_filename=filename,
            label=label,
        )
    except WorkflowPublicationError as error:
        return {"ok": False, "error": str(error)}, 400

    return {
        "ok": True,
        **status,
        "has_unpublished_changes": False,
    }, 201

@app.route("/knowledge")
def knowledge_center():
    """
    Display the Gnojo Knowledge Center.
    """

    return render_template(
        "knowledge_center.html",
        draft_count=knowledge_repository.count_drafts(),
        published_count=knowledge_repository.count_published(),
    )

@app.route("/knowledge/builder")
def article_builder():
    return render_template(
        "article_builder.html"
    )

@app.route(
    "/commands/builder",
    methods=["GET", "POST"],
)
@app.route(
    "/commands/builder",
    methods=["GET", "POST"],
)
@app.route(
    "/commands/builder",
    methods=["GET", "POST"],
)
def command_builder():
    """
    Create a new Gnojo command draft.
    """

    global current_draft

    draft = current_draft
    completeness = 0

    if draft is not None:
        completeness = (
            draft_generation_service.calculate_completeness(
                draft
            )
        )

    if request.method == "POST":
        command_name = request.form.get(
            "command_name",
            "",
        ).strip()

        description = request.form.get(
            "description",
            "",
        ).strip()

        if command_name:
            draft = (
                draft_generation_service.generate_command_draft(
                    command_name,
                    description,
                    use_generated_content=True,
                )
            )

            current_draft = draft

            completeness = (
                draft_generation_service.calculate_completeness(
                    draft
                )
            )

    return render_template(
        "command_builder.html",
        draft=draft,
        completeness=completeness,
    )

@app.route(
    "/commands/builder/edit",
    methods=["GET", "POST"],
)
def edit_command_draft():
    """
    Edit the most recently generated command draft.
    """

    global current_draft

    if current_draft is None:
        return redirect(
            url_for("command_builder")
        )

    if request.method == "POST":
        current_draft["command_name"] = request.form.get(
            "command_name",
            "",
        ).strip()

        current_draft["summary"] = request.form.get(
            "summary",
            "",
        ).strip()

        current_draft["syntax"] = request.form.get(
            "syntax",
            "",
        ).strip()
        updated_examples = []

        for index, example in enumerate(
            current_draft.get("examples", []),
            start=1,
        ):
            command_value = request.form.get(
                f"example_command_{index}",
                "",
            ).strip()

            description_value = request.form.get(
                f"example_description_{index}",
                "",
            ).strip()

            updated_examples.append(
                {
                    "command": command_value,
                    "description": description_value,
                }
            )

        current_draft["examples"] = updated_examples

        updated_fields = []

        for index, field in enumerate(
            current_draft.get("important_fields", []),
            start=1,
        ):
            field_name = request.form.get(
                f"field_name_{index}",
                "",
            ).strip()

            field_description = request.form.get(
                f"field_description_{index}",
                "",
            ).strip()

            updated_fields.append(
                {
                    "field": field_name,
                    "description": field_description,
                }
            )

        current_draft["important_fields"] = updated_fields
        updated_errors = []

        for index, error in enumerate(
            current_draft.get("common_errors", []),
            start=1,
        ):
            error_title = request.form.get(
                f"error_title_{index}",
                "",
            ).strip()

            error_description = request.form.get(
                f"error_description_{index}",
                "",
            ).strip()

            updated_errors.append(
                {
                    "error": error_title,
                    "description": error_description,
                }
            )

        current_draft["common_errors"] = updated_errors

        updated_related_commands = []

        for index, command in enumerate(
            current_draft.get("related_commands", []),
            start=1,
        ):
            command_value = request.form.get(
                f"related_command_{index}",
                "",
            ).strip()

            if command_value:
                updated_related_commands.append(
                    command_value
                )

        current_draft[
            "related_commands"
        ] = updated_related_commands

        updated_references = []

        for index, reference in enumerate(
            current_draft.get(
                "official_references",
                [],
            ),
            start=1,
        ):
            reference_title = request.form.get(
                f"reference_title_{index}",
                "",
            ).strip()

            reference_url = request.form.get(
                f"reference_url_{index}",
                "",
            ).strip()

            updated_references.append(
                {
                    "title": reference_title,
                    "url": reference_url,
                }
            )

        current_draft[
            "official_references"
        ] = updated_references
        explanation = current_draft["explanation"]

        explanation.purpose = request.form.get(
            "explanation_purpose",
            "",
        ).strip()

        explanation.when_to_use = request.form.get(
            "explanation_when_to_use",
            "",
        ).strip()

        explanation.narrative = request.form.get(
            "explanation_narrative",
            "",
        ).strip()

        explanation.permissions_notes = request.form.get(
            "explanation_permissions_notes",
            "",
        ).strip()

        explanation.risk_level = request.form.get(
            "explanation_risk_level",
            "Unknown",
        ).strip()

        explanation.risk_warning = request.form.get(
            "explanation_risk_warning",
            "",
        ).strip()

        current_draft["metadata"].touch()

        return redirect(
            url_for("edit_command_draft")
        )

    return render_template(
        "command_builder_edit.html",
        draft=current_draft,
    )

@app.route(
    "/commands/builder/publish",
    methods=["POST"],
)
def publish_command_draft():
    """
    Validate the current command draft for publication.
    """

    if current_draft is None:
        return redirect(
            url_for("command_builder")
        )

    is_valid, missing_sections = (
        publish_validation_service.validate_command_draft(
            current_draft
        )
    )

    if not is_valid:
        return render_template(
            "publish_validation.html",
            draft=current_draft,
            missing_sections=missing_sections,
        )

    publication_category = request.form.get(
        "publication_category",
        "Networking",
    )

    published_article = publication_service.publish(
        current_draft,
        category=publication_category,
    )

    return render_template(
        "published_command.html",
        article=published_article,
    )

@app.route("/knowledge/articles/current")
def view_published_article():
    """
    Display the most recently published article.
    """

    if current_draft is None:
        return redirect(
            url_for("knowledge_center")
        )

    is_valid, missing_sections = (
        publish_validation_service.validate_command_draft(
            current_draft
        )
    )

    if not is_valid:
        return redirect(
            url_for("edit_command_draft")
        )

    article = publication_service.publish(
        current_draft
    )

    published_article = publication_service.publish(
        current_draft
    )

    return render_template(
        "published_article.html",
        article=article,
    )

@app.route("/commands")
def list_commands():
    """
    Display all commands grouped by category.
    """

    commands = command_repository.get_all()

    grouped_commands = {}

    for command in commands:
        category = command.get(
            "category",
            "Uncategorized",
        )

        grouped_commands.setdefault(
            category,
            [],
        ).append(command)

    return render_template(
        "commands.html",
        grouped_commands=grouped_commands,
    )


@app.route("/commands/<command_id>")
def view_command(command_id):
    """
    Display one command from the Command Library.
    """

    command = command_repository.get(command_id)

    if command is None:
        abort(404)

    related_articles = (
        relationship_service.related_articles_for_command(
            command_id
        )
    )

    related_commands = (
        relationship_service.related_commands_for_command(
            command_id
        )
    )

    explanation = explanation_service.explain_command(
    command,
    related_commands,
)

    return render_template(
        "command.html",
        command=command,
        related_articles=related_articles,
        related_commands=related_commands,
        explanation=explanation,
    )


@app.route("/scripts")
def list_scripts():
    scripts = script_repository.get_all()
    return render_template(
        "scripts.html",
        automations=[item for item in scripts if item.get("kind") == "Automation"],
        collectors=[item for item in scripts if item.get("kind") != "Automation"],
    )


@app.route("/scripts/builder", methods=["GET", "POST"])
def script_builder():
    draft = None
    errors = []
    validated = False
    if request.method == "POST":
        def lines(name):
            return [item.strip() for item in request.form.get(name, "").splitlines() if item.strip()]

        parameters = []
        for index, value in enumerate(lines("parameters"), start=1):
            parts = [item.strip() for item in value.split("|", 2)]
            if len(parts) != 3 or parts[1].lower() not in {"required", "optional"}:
                errors.append(f"Parameter line {index} must use: name | required or optional | description")
                continue
            parameters.append({"name": parts[0].lstrip("-"), "required": parts[1].lower() == "required", "description": parts[2]})
        draft = {
            "id": request.form.get("script_id", "").strip(), "name": request.form.get("name", "").strip(),
            "kind": request.form.get("kind", "Diagnostic Collector"), "summary": request.form.get("summary", "").strip(),
            "platform": request.form.get("platform", "Windows"), "language": request.form.get("language", "PowerShell"),
            "category": request.form.get("category", "").strip(), "source": request.form.get("source", ""),
            "collects": lines("collects"), "changes": lines("changes"), "parameters": parameters,
            "dry_run": request.form.get("dry_run", "").strip(), "rollback": request.form.get("rollback", "").strip(),
            "requires_elevation": request.form.get("requires_elevation") == "on",
            "permission_notes": request.form.get("permission_notes", "").strip(),
            "privacy_note": request.form.get("privacy_note", "").strip(),
            "related_commands": [item.strip() for item in request.form.get("related_commands", "").split(",") if item.strip()],
            "related_workflows": [item.strip() for item in request.form.get("related_workflows", "").split(",") if item.strip()],
        }
        existing_ids = {item.get("id") for item in script_repository.get_all()}
        errors.extend(script_authoring_service.validate(draft, existing_ids))
        known_commands = {item.get("id") for item in command_repository.get_all()}
        unknown_commands = [item for item in draft["related_commands"] if item not in known_commands]
        if unknown_commands:
            errors.append("Unknown related command IDs: " + ", ".join(unknown_commands))
        known_workflows = set(available_workflows())
        unknown_workflows = [item for item in draft["related_workflows"] if item not in known_workflows]
        if unknown_workflows:
            errors.append("Unknown related workflow IDs: " + ", ".join(unknown_workflows))
        validated = not errors
        if validated and request.form.get("action") == "publish":
            try:
                record = script_authoring_service.publish(draft, existing_ids)
            except (OSError, ValueError, ScriptAuthoringError) as error:
                errors.append(str(error))
                validated = False
            else:
                return redirect(url_for("view_script", script_id=record["id"], published="1"))
    return render_template("script_builder.html", draft=draft, errors=errors, validated=validated)


@app.route("/scripts/<script_id>")
def view_script(script_id):
    script = script_repository.get(script_id)
    if script is None:
        abort(404)
    related_commands = [command_repository.get(item) for item in script.get("related_commands", [])]
    return render_template("script.html", script=script, related_commands=[item for item in related_commands if item])


@app.route("/scripts/<script_id>/download")
def download_script(script_id):
    script = script_repository.get(script_id)
    if script is None:
        abort(404)
    return send_file(script_repository.source_path(script), as_attachment=True, download_name=script["filename"], mimetype="text/plain")

@app.route("/search/test")
def search_test():
    """
    Temporarily test universal search results as JSON.
    """

    query = request.args.get(
        "q",
        "",
    ).strip()

    if not query:
        return {
            "query": "",
            "articles": [],
            "commands": [],
        }

    results = search_service.search(query)

    return {
        "query": query,
        "articles": [
            article.id
            for article in results["articles"]
        ],
        "commands": [
            command.id
            for command in results["commands"]
        ],
        "workflows": [workflow.id for workflow in results["workflows"]],
    }

@app.route("/search")
def search():
    """
    Display universal search results.
    """

    query = request.args.get(
        "q",
        "",
    ).strip()

    selected_type = request.args.get(
        "type",
        "all",
    ).strip().lower()

    device_context = active_device_profile()
    results = search_service.search_all(query, context=device_context)
    result_counts = {
        "all": len(results),
        "article": sum(result.content_type == "Article" for result in results),
        "command": sum(result.content_type == "Command" for result in results),
        "workflow": sum(result.content_type == "Workflow" for result in results),
    }

    if selected_type == "article":
        results = [
            result
            for result in results
            if result.content_type == "Article"
        ]

    elif selected_type == "command":
        results = [
            result
            for result in results
            if result.content_type == "Command"
        ]
    elif selected_type == "workflow":
        results = [result for result in results if result.content_type == "Workflow"]
    elif selected_type != "all":
        selected_type = "all"

    return render_template(
        "search_results.html",
        query=query,
        results=results,
        selected_type=selected_type,
        result_counts=result_counts,
        active_device=device_context,
    )

@app.route("/api/search/suggestions")
def search_suggestions():
    """
    Return lightweight search suggestions for the global search bar.
    """

    query = request.args.get(
        "q",
        "",
    ).strip()

    if len(query) < 2:
        return {
            "suggestions": [],
        }

    results = search_service.search_all(query, context=active_device_profile())

    suggestions = []

    for result in results[:8]:
        suggestions.append(
            {
                "id": result.id,
                "title": result.title,
                "summary": result.summary,
                "content_type": result.content_type,
                "endpoint": result.endpoint,
            }
        )

    return {
        "suggestions": suggestions,
    }

@app.route("/knowledge/drafts")
def list_drafts():
    """
    Display all draft knowledge articles awaiting review.
    """

    drafts = knowledge_repository.get_drafts()

    return render_template(
        "drafts.html",
        drafts=drafts,
    )

def workflow_references_for_article(article_id):
    references = []
    seen = set()
    draft_map = {
        item.get("workflow_id"): item.get("filename")
        for item in WorkflowDraftService().list_drafts()
        if item.get("workflow_id") and not item.get("is_damaged")
    }
    catalog = available_workflows()
    for workflow_id, details in catalog.items():
        engine = DecisionEngine()
        try:
            load_runtime_workflow(engine, workflow_id, catalog, details.get("version"))
        except (FileNotFoundError, WorkflowPublicationError, ValueError):
            continue
        for node_id, node in engine.workflow.get("nodes", {}).items():
            if isinstance(node, dict) and node.get("knowledge_article") == article_id:
                key = (workflow_id, node_id)
                if key in seen:
                    continue
                seen.add(key)
                references.append({
                    "workflow_id": workflow_id,
                    "workflow_name": details.get("name") or workflow_id,
                    "node_id": node_id,
                    "node_title": node.get("title") or node.get("question") or node_id.replace("_", " ").title(),
                    "filename": draft_map.get(workflow_id),
                })
    return references


def render_article_review(article, error=None, status=200):
    return render_template(
        "draft_review.html",
        article=article,
        analysis=ArticleReviewService().analyze(article),
        workflow_references=workflow_references_for_article(article.get("id")),
        workflow_return_url=workflow_return_location(article),
        error=error,
    ), status


def workflow_return_location(article):
    """Return a safe editor URL for an article created from a workflow node."""
    origin = article.get("workflow_origin") if isinstance(article, dict) else None
    if not isinstance(origin, dict):
        return None
    filename = origin.get("filename")
    node_id = origin.get("node_id")
    if not isinstance(filename, str) or not isinstance(node_id, str):
        return None
    try:
        workflow = WorkflowDraftService().get_draft(filename)
    except (WorkflowDraftError, ValueError):
        return None
    if not isinstance(workflow, dict) or node_id not in workflow.get("nodes", {}):
        return None
    return url_for(
        "workflow_editor",
        filename=filename,
        node=node_id,
        article_published=article.get("id"),
    )


@app.route("/knowledge/drafts/<article_id>", methods=["GET", "POST"])
def review_draft(article_id):
    """
    Display a draft article for review.
    """

    try:
        article = knowledge_repository.get_draft(article_id)

    except ArticleNotFoundError:
        abort(404)

    except KnowledgeRepositoryError:
        abort(500)

    if request.method == "POST":
        try:
            article = ArticleReviewService().update_from_form(article, request.form)
            knowledge_repository.save_draft(article, overwrite=True)
            if request.form.get("review_action") == "approve_and_publish":
                analysis = ArticleReviewService().analyze(article)
                if not analysis["can_publish"]:
                    return render_article_review(
                        article,
                        "Complete validation and every technical review check before publishing.",
                        400,
                    )
                return_location = workflow_return_location(article)
                knowledge_repository.publish_article(article_id, overwrite=True)
                return redirect(return_location or url_for("view_published", article_id=article_id))
        except (ArticleReviewError, KnowledgeRepositoryError) as error:
            return render_article_review(article, str(error), 400)
        return redirect(url_for("review_draft", article_id=article_id, saved="1"))

    return render_article_review(article)


@app.post("/api/knowledge/drafts/<article_id>/source-suggestions")
def find_article_source_suggestions(article_id):
    try:
        article = knowledge_repository.get_draft(article_id)
        result = ArticleSourceFinderService().find(article)
    except ArticleNotFoundError:
        return {"ok": False, "error": "Article draft not found."}, 404
    except (ArticleSourceFinderError, KnowledgeRepositoryError) as error:
        return {"ok": False, "error": str(error)}, 400
    return {"ok": True, **result}

@app.route("/knowledge/published")
def list_published():
    """
    Display published articles grouped by category.
    """

    query = request.args.get(
        "q",
        "",
    ).strip().lower()

    articles = knowledge_repository.get_published()
    for article in articles:
        if not article.get("tags"):
            article["tags"] = ArticleTagService.generate(article)

    if query:

        filtered = []

        for article in articles:

            tags = article.get("tags", [])

            if not isinstance(tags, list):
                tags = []

            searchable_text = " ".join(
                [
                    article.get("title", ""),
                    article.get("overview", ""),
                    article.get("category", ""),
                    article.get("difficulty", ""),
                    " ".join(str(tag) for tag in tags),
                ]
            ).lower()

            if query in searchable_text:
                filtered.append(article)

        articles = filtered

    grouped_articles = {}

    for article in articles:

        category = article.get(
            "category",
            "Uncategorized",
        )

        grouped_articles.setdefault(
            category,
            [],
        ).append(article)

    return render_template(
        "published.html",
        grouped_articles=grouped_articles,
        query=query,
    )


@app.route("/knowledge/published/<article_id>")
def view_published(article_id):
    """
    Display one published knowledge article.
    """

    try:
        article = knowledge_repository.get_published_article(
            article_id
        )

        related_articles = []

        for related_id in article.get(
            "related_articles",
            [],
        ):
            try:
                related_article = (
                    knowledge_repository.get_published_article(
                        related_id
                    )
                )

                related_articles.append(
                    related_article
                )

            except ArticleNotFoundError:
                continue

        related_commands = (
            relationship_service.related_commands_for_article(
                article_id
            )
        )

    except ArticleNotFoundError:
        abort(404)

    except KnowledgeRepositoryError:
        abort(500)

    template_name = "published_article.html"

    if article.get("type") == "command":
        template_name = "published_command.html"

    return render_template(
        template_name,
        article=article,
        related_articles=related_articles,
        related_commands=related_commands,
    )


@app.post("/knowledge/published/<article_id>/revise")
def revise_published_article(article_id):
    """Create a reviewable draft without taking the published article offline."""
    try:
        try:
            knowledge_repository.get_draft(article_id)
        except ArticleNotFoundError:
            article = deepcopy(
                knowledge_repository.get_published_article(article_id)
            )
            article["tags"] = ArticleTagService.generate(article)
            article["checklist"] = ArticleReviewService.normalize_checklist(
                article.get("checklist", [])
            )
            review = dict(article.get("review") or {})
            review["status"] = "draft"
            review["checks"] = {
                key: False for key in ArticleReviewService.CHECKS
            }
            review["notes"] = ["Revision created from the published article."]
            article["review"] = review
            knowledge_repository.save_draft(article)
    except ArticleNotFoundError:
        abort(404)
    except KnowledgeRepositoryError:
        abort(500)

    return redirect(
        url_for("review_draft", article_id=article_id, revision="1")
    )

@app.route(
    "/knowledge/drafts/<article_id>/publish",
    methods=["POST"],
)
def publish_draft(article_id):
    """
    Approve a draft and move it into the published library.
    """

    try:
        article = knowledge_repository.get_draft(article_id)

        analysis = ArticleReviewService().analyze(article)
        if not analysis["can_publish"]:
            return render_article_review(
                article,
                "Complete validation, approve the review checklist, and resolve remaining errors before publishing.",
                400,
            )

        knowledge_repository.publish_article(article_id, overwrite=True)

    except ArticleNotFoundError:
        abort(404)

    except KnowledgeRepositoryError:
        abort(500)

    return redirect(
        url_for("knowledge_center")
    )

@app.route(
    "/workflow-builder",
    methods=["GET", "POST"],
)
def workflow_builder():

    generated_workflow = None
    validation = None
    outline = None
    error = None
    filename = None
    form_values = {
        "workflow_name": "",
        "description": "",
        "platform": "Windows",
        "difficulty": "Intermediate",
        "size": "Medium",
    }

    if request.method == "POST":

        form_values = {
            "workflow_name": request.form.get("workflow_name", ""),
            "description": request.form.get("description", ""),
            "platform": request.form.get("platform", "Windows"),
            "difficulty": request.form.get("difficulty", "Beginner"),
            "size": request.form.get("size", "Medium"),
        }

        try:

            engine = WorkflowGenerationEngine()

            generated_workflow = engine.generate_workflow(
                workflow_name=request.form.get(
                    "workflow_name",
                    "",
                ),
                description=request.form.get(
                    "description",
                    "",
                ),
                platform=request.form.get(
                    "platform",
                    "Windows",
                ),
                difficulty=request.form.get(
                    "difficulty",
                    "Beginner",
                ),
                size=request.form.get(
                    "size",
                    "Medium",
                ),
            )

            validator = WorkflowValidationService()

            validation = validator.validate(
                generated_workflow
            )

            outline_service = WorkflowOutlineService()

            outline = outline_service.build_outline(
                generated_workflow
            )

            if validation["is_valid"]:

                draft_service = (
                    WorkflowDraftService()
                )

                filename = (
                    draft_service.save_draft(
                        generated_workflow
                    )
                )
                return redirect(
                    url_for("workflow_builder_result", filename=filename)
                )

        except Exception as ex:

            error = str(ex)

    return render_template(
        "workflow_builder.html",
        generated_workflow=generated_workflow,
        validation=validation,
        outline=outline,
        filename=filename,
        error=error,
        form_values=form_values,
    )


@app.route("/workflow-builder/result/<filename>")
def workflow_builder_result(filename):
    workflow = WorkflowDraftService().get_draft(filename)
    if workflow is None:
        abort(404)
    validation = WorkflowValidationService().validate(workflow)
    outline = WorkflowOutlineService().build_outline(workflow)
    return render_template(
        "workflow_builder.html",
        generated_workflow=workflow,
        validation=validation,
        outline=outline,
        filename=filename,
        error=None,
        form_values={
            "workflow_name": workflow.get("name", ""),
            "description": workflow.get("description", ""),
            "platform": workflow.get("platform", "Windows"),
            "difficulty": workflow.get("difficulty", "Intermediate"),
            "size": workflow.get("size", "Medium"),
        },
    )

@app.route("/wizard", methods=["GET", "POST"])
def wizard():
    engine = DecisionEngine()
    knowledge = KnowledgeBase()
    workflow_catalog = available_workflows()
    if request.method == "GET" and request.args.get("learning") in {"0", "1"}:
        session["learning_mode"] = request.args.get("learning") == "1"
        if not session["learning_mode"]:
            session.pop("learning_concepts", None)

    # --------------------------------------------------
    # Process an answer or continue an instruction
    # --------------------------------------------------
    if request.method == "POST":
        workflow_name = session.get("workflow")
        current_node_id = session.get("current_node")

        if (
            workflow_name not in workflow_catalog
            or current_node_id is None
        ):
            return redirect(url_for("home"))

        try:
            load_runtime_workflow(engine, workflow_name, workflow_catalog, session.get("workflow_version"))
        except (FileNotFoundError, WorkflowPublicationError, ValueError):
            abort(404)

        current_node = engine.get_node(current_node_id)

        if current_node is None:
            return redirect(url_for("home"))

        navigation_action = request.form.get(
            "navigation_action"
        )

        if navigation_action == "previous":
            node_history = session.get(
                "node_history",
                [],
        )

            if node_history:
                previous_location = node_history.pop()

                previous_workflow = previous_location["workflow"]
                previous_node_id = previous_location["node_id"]
                previous_version = previous_location.get("version")
            else:
                return redirect(url_for("wizard", workflow=workflow_name, resume="1"))

            if previous_workflow not in workflow_catalog:
                abort(404)

            try:
                load_runtime_workflow(engine, previous_workflow, workflow_catalog, previous_version)
            except (FileNotFoundError, WorkflowPublicationError, ValueError):
                abort(404)

            previous_node = engine.get_node(previous_node_id)

            if previous_node is None:
                abort(404)

            session["node_history"] = node_history
            session["workflow"] = previous_workflow
            session["current_node"] = previous_node_id
            session["workflow_version"] = previous_version
            session["step"] = max(
                session.get("step", 1) - 1,
                1,
            )
            track_history_progress(
                previous_node_id,
                action="back",
                workflow_id=previous_workflow,
                workflow_name=workflow_catalog[previous_workflow]["name"],
                version=previous_version,
            )

            return redirect(
                url_for(
                    "wizard",
                    workflow=previous_workflow,
                    resume="1",
                )
            )

        if current_node.type == "transition":
            next_workflow = current_node.next_workflow

            if next_workflow not in workflow_catalog:
                abort(404)

            try:
                next_version = workflow_catalog[next_workflow].get("version") if workflow_catalog[next_workflow].get("source") == "published" else None
                load_runtime_workflow(engine, next_workflow, workflow_catalog, next_version)
            except (FileNotFoundError, WorkflowPublicationError, ValueError):
                abort(404)

            try:
                next_node, skipped = resolve_applicable_node(engine, engine.workflow.get("start_node"), active_device_profile())
            except WorkflowConditionError:
                abort(500)
            session["skipped_nodes"] = skipped

            if next_node is None:
                abort(500)

            node_history = session.get(
                "node_history",
                [],
            )

            node_history.append(
            {
                    "workflow": workflow_name,
                    "node_id": current_node.id,
                    "version": session.get("workflow_version"),
                }
            )

            session["node_history"] = node_history
            session["workflow"] = next_workflow
            session["current_node"] = next_node.id
            session["workflow_version"] = next_version
            session["step"] = 0
            track_history_progress(
                next_node.id,
                action="transition",
                workflow_id=next_workflow,
                workflow_name=workflow_catalog[next_workflow]["name"],
                version=next_version,
            )

            return redirect(
                url_for(
                    "wizard",
                    workflow=next_workflow,
                    resume="1",
                )
            )

        answer = request.form.get("answer")
        next_node = engine.advance(
            current_node,
            answer,
        )

        if next_node is not None:
            try:
                next_node, skipped = resolve_applicable_node(engine, next_node.id, active_device_profile())
            except WorkflowConditionError:
                abort(500)
            session["skipped_nodes"] = skipped

        if next_node is not None:
            node_history = session.get(
                "node_history",
                [],
            )

            node_history.append(
                {
                    "workflow": workflow_name,
                    "node_id": current_node.id,
                    "version": session.get("workflow_version"),
                }
            )
        
            session["node_history"] = node_history
            session["current_node"] = next_node.id

            estimated_steps = engine.workflow.get(
                "estimated_steps",
                5,
            )

            current_step = session.get("step", 1)

            session["step"] = min(
                current_step + 1,
                estimated_steps,
            )
            track_history_progress(next_node.id)

        return redirect(
            url_for(
                "wizard",
                workflow=workflow_name,
                resume="1",
            )
        )

    # --------------------------------------------------
    # Resume the current workflow after a redirect
    # --------------------------------------------------
    workflow_name = request.args.get("workflow")
    resume_workflow = request.args.get("resume") == "1"

    if resume_workflow:
        session_workflow = session.get("workflow")
        current_node_id = session.get("current_node")

        if (
            workflow_name != session_workflow
            or workflow_name not in workflow_catalog
            or current_node_id is None
        ):
            return redirect(url_for("home"))

        try:
            load_runtime_workflow(engine, workflow_name, workflow_catalog, session.get("workflow_version"))
        except (FileNotFoundError, WorkflowPublicationError, ValueError):
            abort(404)

        try:
            node, skipped = resolve_applicable_node(engine, current_node_id, active_device_profile())
        except WorkflowConditionError:
            abort(500)
        if node and node.id != current_node_id:
            session["current_node"] = node.id
        if skipped:
            session["skipped_nodes"] = skipped

        if node is None:
            return redirect(url_for("home"))

        return render_wizard(
            engine,
            node,
            knowledge,
            workflow_catalog,
        )

    # --------------------------------------------------
    # Start or restart a workflow
    # --------------------------------------------------
    if workflow_name not in workflow_catalog:
        return redirect(url_for("home"))

    existing_session = active_troubleshooting_session(workflow_catalog)
    restarting = request.args.get("restart") == "1"
    if existing_session and not restarting:
        return render_template(
            "workflow_recovery.html",
            active_session=existing_session,
            requested_workflow_id=workflow_name,
            requested_workflow=workflow_catalog[workflow_name],
            same_workflow=existing_session["workflow_id"] == workflow_name,
        )
    if existing_session and restarting:
        abandon_active_history()

    device = active_device_profile()
    compatibility = workflow_device_compatibility(workflow_catalog[workflow_name], device)
    if compatibility == "incompatible" and request.args.get("override") != "1":
        return render_template(
            "workflow_compatibility.html",
            workflow_id=workflow_name,
            workflow=workflow_catalog[workflow_name],
            active_device=device,
        )

    try:
        pinned_version = workflow_catalog[workflow_name].get("version") if workflow_catalog[workflow_name].get("source") == "published" else None
        load_runtime_workflow(engine, workflow_name, workflow_catalog, pinned_version)
    except (FileNotFoundError, WorkflowPublicationError, ValueError):
        abort(404)

    try:
        node, skipped = resolve_applicable_node(engine, engine.workflow.get("start_node"), active_device_profile())
    except WorkflowConditionError:
        abort(500)

    if node is None:
        abort(500)

    session["workflow"] = workflow_name
    session["workflow_version"] = pinned_version
    session["workflow_complete"] = False
    session["current_node"] = node.id
    session["step"] = 1
    session["node_history"] = []
    session["skipped_nodes"] = skipped
    if session.get("learning_mode"):
        session["learning_concepts"] = []

    try:
        history_record = TroubleshootingHistoryService().start(
            workflow_name,
            workflow_catalog[workflow_name]["name"],
            node.id,
            version=pinned_version,
            device=active_device_profile(),
            learning_mode=session.get("learning_mode", False),
        )
        session["troubleshooting_history_id"] = history_record["id"]
    except OSError:
        app.logger.warning("Unable to create troubleshooting history.")
        session.pop("troubleshooting_history_id", None)

    return render_wizard(
        engine,
        node,
        knowledge,
        workflow_catalog,
    )


def render_wizard(engine, node, knowledge, workflow_catalog=None):
    """
    Render the shared wizard template with workflow progress
    and optional knowledge article content.
    """

    workflow_name = session["workflow"]
    workflow_info = (workflow_catalog or available_workflows())[workflow_name]

    estimated_steps = engine.workflow.get("estimated_steps", 5)
    current_step = session.get("step", 1)
    current_step = max(current_step, 1)

    history_record = None
    if node.type == "resolution":
        session["workflow_complete"] = True
        current_step = estimated_steps
        progress_percent = 100
        history_id = session.get("troubleshooting_history_id")
        if history_id:
            try:
                history_record = TroubleshootingHistoryService().complete(
                    history_id,
                    node.id,
                    getattr(node, "title", None) or getattr(node, "message", None),
                )
            except (OSError, TroubleshootingHistoryError):
                app.logger.warning("Unable to complete troubleshooting history.")
    else:
        current_step = min(current_step, estimated_steps)

        progress_percent = min(
            round((current_step / estimated_steps) * 100),
            100,
        )

    article = None

    if node.knowledge_article:
        article = knowledge.load_article(
            node.knowledge_article
        )

    learning_mode = session.get("learning_mode", False)
    learning_content = None
    concepts_covered = session.get("learning_concepts", [])
    if learning_mode:
        learning_content = LearningModeService().build(node, workflow_info["name"], article)
        for concept in learning_content["concepts"]:
            if concept["title"] not in concepts_covered:
                concepts_covered.append(concept["title"])
        session["learning_concepts"] = concepts_covered

    return render_template(
    "wizard.html",
    node=node,
    article=article,
    workflow_id=workflow_name,
    workflow_name=workflow_info["name"],
    active_device=active_device_profile(),
    learning_mode=learning_mode,
    learning_content=learning_content,
    concepts_covered=concepts_covered,
    skipped_nodes=session.pop("skipped_nodes", []),
    current_step=current_step,
    estimated_steps=estimated_steps,
    progress_percent=progress_percent,
    can_go_back=bool(
        session.get("node_history")
    ),
    history_record=history_record,
)


if __name__ == "__main__":
    app.run(debug=os.getenv("GNOJO_DEBUG", "false").lower() in {"1", "true", "yes"})
