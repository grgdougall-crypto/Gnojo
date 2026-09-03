import json
import os
import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
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
from app.services.article_identity_resolver import ArticleIdentityResolver

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
from app.services.workflow_runtime_compatibility_service import (
    apply_runtime_compatibility_handoffs,
)
from app.services.workflow_lifecycle_projection_service import (
    WorkflowLifecycleProjectionService,
)
from app.repositories.workflow_publication_review_repository import (
    WorkflowPublicationReasoningReview,
    WorkflowPublicationReviewRepository,
    WorkflowPublicationReviewRepositoryError,
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
from app.services.workflow_progress_service import WorkflowProgressService
from app.services.device_profile_service import DeviceProfileError, DeviceProfileService
from app.services.workflow_condition_service import WorkflowConditionError, resolve_applicable_node
from app.services.learning_mode_service import LearningModeService
from app.services.troubleshooting_history_service import (
    TroubleshootingHistoryError,
    TroubleshootingHistoryService,
)
TROUBLESHOOTING_SESSION_ENVIRONMENTS = frozenset(
    TroubleshootingHistoryService.SESSION_ENVIRONMENTS
)
from app.services.content_quality_service import ContentQualityService
from app.services.curator_content_quality_bridge_service import (
    CuratorContentQualityBridgeError,
    CuratorContentQualityBridgeService,
)
from app.services.curator_confusing_step_improvement_service import (
    CuratorConfusingStepImprovementError,
    CuratorConfusingStepImprovementService,
)
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_task_review_presentation_service import CuratorTaskReviewPresentationService
from app.services.curator_task_service import CuratorTaskService
from app.services.curator_task_navigation_service import CuratorTaskNavigationService
from app.services.curator_relationship_proposal_queue_service import CuratorRelationshipProposalQueueService
from app.services.curator_relationship_repair_application_service import (
    CuratorRelationshipRepairApplicationError,
    CuratorRelationshipRepairApplicationService,
)
from app.services.curator_relationship_repair_browser_harness import phase3_browser_harness
from app.services.curator_resolution_service import CuratorResolutionService
from app.services.curator_batch_service import CuratorBatchService
from app.services.curator_fix_session_service import CuratorFixSessionError, CuratorFixSessionService
from app.services.curator_repair_executor import CuratorRepairError, CuratorRepairExecutor
from app.services.curator_repair_planner import CuratorRepairPlanner
from app.services.curator_session_reconciliation_service import CuratorSessionReconciliationService
from app.services.curator_targeted_verification_service import CuratorTargetedVerificationService
from app.services.curator_verification_presentation_service import CuratorVerificationPresentationService
from app.services.curator_progress_auto_repair_policy_service import (
    CuratorProgressAutoRepairPolicyService,
)
from app.services.curator_growth_service import CuratorGrowthService
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)
from app.services.knowledge_source_research_service import (
    KnowledgeSourceResearchError,
    KnowledgeSourceResearchService,
)
from app.services.knowledge_evidence_extraction_service import (
    KnowledgeEvidenceExtractionError,
    KnowledgeEvidenceExtractionService,
)
from app.services.knowledge_draft_generation_service import (
    KnowledgeDraftGenerationError,
    KnowledgeDraftGenerationService,
)
from app.services.knowledge_draft_refinement_service import (
    KnowledgeDraftRefinementError,
    KnowledgeDraftRefinementService,
)
from app.services.knowledge_claim_planning_service import (
    KnowledgeClaimPlanningError,
    KnowledgeClaimPlanningService,
)
from app.services.knowledge_draft_assembly_service import (
    KnowledgeDraftAssemblyError,
    KnowledgeDraftAssemblyService,
)
from app.services.knowledge_workflow_generation_service import (
    KnowledgeWorkflowGenerationError,
    KnowledgeWorkflowGenerationService,
)
from app.services.knowledge_campaign_orchestration_service import (
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.campaign_blocker_destination_service import CampaignBlockerDestinationService
from curator.locking import AuditAlreadyRunningError
from curator.governance import CuratorGovernanceError
from curator.growth import CuratorGrowthError
from curator.memory import CuratorMemoryError, CuratorMemoryStore
from curator.resolution import ResolutionPackageError
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
from app.services.knowledge_publication_service import KnowledgePublicationError, KnowledgePublicationService
from app.services.knowledge_integrity_service import KnowledgeIntegrityError, KnowledgeIntegrityService
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


def safe_internal_return(value, allowed_prefixes):
    """Accept a local return URL only when it belongs to an expected journey."""
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or parsed.scheme
        or parsed.netloc
    ):
        return ""
    path = parsed.path
    return candidate if any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in allowed_prefixes
    ) else ""
relationship_service = RelationshipService()
explanation_service = ExplanationService()
draft_generation_service = DraftGenerationService()
publish_validation_service = PublishValidationService()
publication_service = PublicationService()
knowledge_publication_service = KnowledgePublicationService(knowledge_repository)


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
    "higher_layer_connectivity": {
        "name": "Higher-Layer Connectivity Diagnostics",
        "description": "Identify browser, VPN, proxy, security-software, and application-specific connectivity conditions.",
        "icon": "bi-diagram-3",
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

    # Preserve immutable historical publications while allowing narrowly scoped
    # runtime compatibility for older active snapshots.
    apply_runtime_compatibility_handoffs(engine, workflow_id)


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
    history_page = service.query_page(
        page=request.args.get("page", "1"),
        workflow=request.args.get("workflow", ""),
        status=request.args.get("status", ""),
        range=request.args.get("range", "all"),
        environment=request.args.get("environment", "production"),
    )
    return render_template(
        "troubleshooting_history.html",
        records=history_page["records"],
        analytics=history_page["analytics"],
        history_page=history_page,
        history_return_to=quote(request.full_path.rstrip("?"), safe=""),
    )


@app.route("/troubleshooting-history/<history_id>")
def troubleshooting_history_detail(history_id):
    record = TroubleshootingHistoryService().get(history_id)
    if record is None:
        abort(404)
    return_to = safe_internal_return(
        request.args.get("return_to", ""), ("/troubleshooting-history",)
    )
    return render_template(
        "troubleshooting_history_detail.html", record=record, return_to=return_to
    )


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
    records = TroubleshootingHistoryService().list(500, environment="production")
    versions = {workflow_id: catalog[workflow_id].get("version") for workflow_id in workflow_data}
    report = ContentQualityService().build(
        workflow_data, records, drafts, workflow_versions=versions
    )
    CuratorContentQualityBridgeService().mark_tracked(report)
    return render_template(
        "content_quality.html", report=report,
        curator_return_context="/content-quality#queueTitle",
    )


@app.post("/content-quality/confusing-step/curator")
def send_confusing_step_to_curator():
    workflow_id = request.form.get("workflow_id", "")
    node_id = request.form.get("node_id", "")
    catalog = available_workflows()
    if workflow_id not in catalog:
        abort(404)
    engine = DecisionEngine()
    try:
        load_runtime_workflow(engine, workflow_id, catalog, catalog[workflow_id].get("version"))
    except (FileNotFoundError, WorkflowPublicationError, ValueError):
        abort(404)
    drafts = {
        item["workflow_id"]: item["filename"]
        for item in WorkflowDraftService().list_drafts()
        if item.get("workflow_id") and not item.get("is_damaged")
    }
    report = ContentQualityService().build(
        {workflow_id: engine.workflow},
        TroubleshootingHistoryService().list(500, environment="production"),
        drafts,
        workflow_versions={workflow_id: catalog[workflow_id].get("version")},
    )
    finding = next(
        (
            item for item in report["action_queue"]
            if item.get("kind") == "confusing_step"
            and item.get("workflow_id") == workflow_id
            and item.get("node_id") == node_id
        ),
        None,
    )
    if finding is None:
        abort(404)
    try:
        task = CuratorContentQualityBridgeService().send(finding)
    except (CuratorContentQualityBridgeError, CuratorGovernanceError):
        abort(400)
    return redirect(url_for("content_quality", curator_task=task["task_id"]))


@app.route("/curator")
def curator_dashboard():
    messages = {
        "completed": ("success", "Curator audit completed. The dashboard now shows the latest operational findings."),
        "running": ("warning", "A Curator audit is already running. Return shortly to review the completed report."),
        "failed": ("danger", "The Curator audit could not be completed. Existing reports and trusted content were not changed."),
        "batch_completed": ("success", "The first Assisted Resolution batch was prepared. No drafts, links, or publications were changed."),
        "batch_failed": ("danger", "The Assisted Resolution batch could not be completed safely."),
        "disposition_updated": ("success", "The reasoning review disposition was recorded as calibration metadata."),
        "disposition_invalid": ("danger", "The reasoning review disposition could not be recorded."),
    }
    notice = request.args.get("notice", "")
    legacy_status = request.args.get("status", "")
    kind, message = messages.get(notice or legacy_status, ("info", ""))
    try:
        filters = {name: request.args.get(name, "") for name in
                   ("status", "include_resolved", "classification", "workflow", "family", "rule", "disposition", "q")}
        if legacy_status in messages:
            filters["status"] = ""
        dashboard = CuratorDashboardService().dashboard(
            sort_by=request.args.get("sort", "debt"), filters=filters)
    except CuratorMemoryError:
        dashboard = {"has_audit": False, "tasks": [], "recent_audits": []}
        kind, message = "danger", "Curator memory could not be read. Existing trusted content was not changed."
    return render_template(
        "curator_dashboard.html", dashboard=dashboard, status_kind=kind, status_message=message,
        assisted_batch=CuratorBatchService().latest(),
    )


@app.get("/curator/relationship-proposals")
def curator_relationship_proposals():
    queue = CuratorRelationshipProposalQueueService().queue(
        outcome=request.args.get("outcome", ""), status=request.args.get("status", "")
    )
    return render_template("curator_relationship_proposals.html", queue=queue)


def _phase3_harness_enabled():
    explicitly_enabled = os.getenv("GNOJO_PHASE3_BROWSER_HARNESS", "").casefold() in {"1", "true", "yes"}
    return explicitly_enabled and (app.testing or app.debug)


def _require_phase3_harness():
    if not _phase3_harness_enabled():
        abort(404)
    return phase3_browser_harness()


@app.get("/__dev/phase3-relationship-harness")
def phase3_relationship_harness_queue():
    harness = _require_phase3_harness()
    queue = CuratorRelationshipProposalQueueService(harness.root).queue(
        outcome=request.args.get("outcome", ""), status=request.args.get("status", "")
    )
    return render_template(
        "curator_relationship_proposals.html", queue=queue, harness_mode=True,
        queue_clear_url=url_for("phase3_relationship_harness_queue"),
    )


@app.post("/__dev/phase3-relationship-harness/reset")
def phase3_relationship_harness_reset():
    _require_phase3_harness().reset()
    return redirect(url_for("phase3_relationship_harness_queue", reset="1"))


@app.post("/curator/tasks/<task_id>/review-disposition")
def curator_task_review_disposition(task_id):
    status = "disposition_updated"
    try:
        CuratorTaskService().update_review_disposition(
            task_id, request.form.get("disposition", ""))
    except (CuratorMemoryError, ValueError):
        status = "disposition_invalid"
    return_to = request.form.get("return_to", "")
    if not return_to.startswith("/curator") or return_to.startswith("//"):
        return_to = url_for("curator_dashboard") + "#knowledge-tasks"
    path, marker, fragment = return_to.partition("#")
    separator = "&" if "?" in path else "?"
    destination = f"{path}{separator}notice={status}"
    if marker:
        destination += f"#{fragment}"
    return redirect(destination)


@app.route("/curator/growth")
def curator_growth_dashboard():
    messages = {
        "updated": ("success", "The human decision was recorded in Curator Memory."),
        "control_updated": ("success", "The Curator operating control was updated and logged."),
        "invalid": ("danger", request.args.get("error") or "The requested growth decision was rejected."),
    }
    kind, message = messages.get(request.args.get("status", ""), ("info", ""))
    try:
        growth = CuratorGrowthService().dashboard()
    except CuratorMemoryError:
        abort(503)
    return render_template("curator_growth.html", growth=growth,
                           status_kind=kind, status_message=message)


@app.post("/curator/growth/<subject_type>/<subject_id>/decision")
def curator_growth_decision(subject_type, subject_id):
    try:
        CuratorGrowthService().decide(
            subject_type, subject_id, request.form.get("status", ""),
            reviewer=request.form.get("reviewer", ""), reason=request.form.get("reason", ""),
        )
        status, error = "updated", ""
    except (CuratorGrowthError, CuratorMemoryError, ValueError) as exception:
        status, error = "invalid", str(exception)
    return redirect(url_for("curator_growth_dashboard", status=status, error=error))


@app.post("/curator/growth/controls")
def curator_growth_control():
    try:
        CuratorGrowthService().set_control(
            request.form.get("control", ""), request.form.get("disabled") == "true",
            reviewer=request.form.get("reviewer", ""), reason=request.form.get("reason", ""),
        )
        status, error = "control_updated", ""
    except (CuratorGrowthError, CuratorMemoryError) as exception:
        status, error = "invalid", str(exception)
    return redirect(url_for("curator_growth_dashboard", status=status, error=error))


@app.route("/curator/growth/coverage-campaigns", methods=["GET", "POST"])
def knowledge_coverage_campaigns():
    planner = KnowledgeCoveragePlannerService()
    error = ""
    if request.method == "POST":
        try:
            campaign = planner.create(
                title=request.form.get("title", ""),
                domain_id=request.form.get("domain", ""),
                objective=request.form.get("objective", ""),
                notes=request.form.get("notes", ""),
            )
            return redirect(url_for("knowledge_coverage_campaign_detail",
                                    campaign_id=campaign["campaign_id"]))
        except KnowledgeCoveragePlannerError as exception:
            error = str(exception)
    return render_template(
        "knowledge_coverage_campaigns.html",
        campaigns=planner.list_campaigns(), domains=planner.domains(), error=error,
    )


@app.get("/curator/growth/coverage-campaigns/<campaign_id>")
def knowledge_coverage_campaign_detail(campaign_id):
    try:
        campaign = KnowledgeCoveragePlannerService().get(campaign_id)
    except KnowledgeCoveragePlannerError:
        abort(404)
    try:
        research_packages = KnowledgeSourceResearchService().list_for_campaign(campaign_id)
    except KnowledgeSourceResearchError:
        research_packages = []
    try:
        draft_packages = KnowledgeDraftGenerationService().list_for_campaign(campaign_id)
    except KnowledgeDraftGenerationError:
        draft_packages = []
    try:
        workflow_packages = KnowledgeWorkflowGenerationService().list_for_campaign(campaign_id)
    except KnowledgeWorkflowGenerationError:
        workflow_packages = []
    blocker_destinations = {}
    blocker_resolver = CampaignBlockerDestinationService()
    workflow_service = KnowledgeWorkflowGenerationService()
    for item in campaign.get("work_items", []):
        if item.get("work_type") not in workflow_service.WORK_TYPES:
            continue
        try:
            eligibility = workflow_service.eligibility(campaign_id, item["work_item_id"])
        except KnowledgeWorkflowGenerationError:
            # Some isolated/test campaign repositories intentionally do not
            # configure the Phase 8 store. Preserve the legacy candidate
            # action when no authoritative eligibility projection is
            # available rather than inventing blocker state in the view.
            continue
        if eligibility.get("eligible"):
            continue
        destination = blocker_resolver.resolve(
            campaign, item,
            {"blocker_type": "workflow_eligibility",
             "explanation": " ".join(eligibility.get("reasons") or [])},
        )
        if destination.get("resolved"):
            destination["url"] = url_for(destination["endpoint"], **destination["route_values"])
        blocker_destinations[item["work_item_id"]] = destination
    return render_template("knowledge_coverage_campaign_detail.html", campaign=campaign,
                           research_packages=research_packages, draft_packages=draft_packages,
                           workflow_packages=workflow_packages,
                           blocker_destinations=blocker_destinations)


@app.get("/curator/growth/coverage-campaigns/<campaign_id>/orchestration")
def knowledge_campaign_orchestration_detail(campaign_id):
    service = KnowledgeCampaignOrchestrationService()
    try:
        orchestration = service.get_or_create(campaign_id)
    except (KnowledgeCampaignOrchestrationError, KnowledgeCoveragePlannerError):
        abort(404)
    blocker_resolver = CampaignBlockerDestinationService()
    try:
        campaign = KnowledgeCoveragePlannerService().get(campaign_id)
    except KnowledgeCoveragePlannerError:
        # Synthetic/test orchestration projections may not have a persisted
        # campaign.  Related blocker navigation is optional display context;
        # the orchestration page itself remains authoritative and usable.
        campaign = {"campaign_id": campaign_id, "work_items": []}
    work_by_id = {item["work_item_id"]: item for item in campaign.get("work_items", [])}
    for item in orchestration.get("work_item_states", []):
        destination = item.get("review_destination") or {}
        if destination.get("resolved"):
            item["review_link"] = url_for(destination["endpoint"], **destination.get("route_values", {}))
        blocker_destination = blocker_resolver.resolve(
            campaign, work_by_id.get(item.get("work_item_id"), {}), item.get("blocker"))
        item["blocker_destination"] = blocker_destination
        if blocker_destination.get("resolved"):
            item["blocker_link"] = url_for(blocker_destination["endpoint"],
                                            **blocker_destination.get("route_values", {}))
    return render_template("knowledge_campaign_orchestration_detail.html",
                           orchestration=orchestration,
                           orchestration_error=request.args.get("orchestration_error", ""),
                           orchestration_notice=request.args.get("orchestration_notice", ""))


@app.post("/curator/growth/orchestration/<orchestration_id>/mode")
def knowledge_campaign_orchestration_mode(orchestration_id):
    service = KnowledgeCampaignOrchestrationService()
    try:
        orchestration = service.set_mode(orchestration_id, request.form.get("mode", ""))
    except KnowledgeCampaignOrchestrationError as exception:
        return redirect(url_for("knowledge_campaign_orchestration_detail",
                                campaign_id=request.form.get("campaign_id", ""),
                                orchestration_error=str(exception)))
    return redirect(url_for("knowledge_campaign_orchestration_detail",
                            campaign_id=orchestration["campaign_id"]))


@app.post("/curator/growth/orchestration/<orchestration_id>/continue")
def knowledge_campaign_orchestration_continue(orchestration_id):
    service = KnowledgeCampaignOrchestrationService()
    campaign_id = request.form.get("campaign_id", "")
    try:
        orchestration = service.continue_campaign(orchestration_id)
        campaign_id = orchestration["campaign_id"]
        outcomes = orchestration.get("execution", {}).get("outcomes", [])
        failed = next((item for item in outcomes if item.get("status") not in {
            "completed", "package_reused"
        }), None)
        error = (failed or {}).get("message", "")
        notice = "" if failed else (
            "Campaign advanced one recommended work item." if outcomes
            else "No machine-ready campaign action is currently available."
        )
    except KnowledgeCampaignOrchestrationError as exception:
        error = str(exception)
        notice = ""
    return redirect(url_for("knowledge_campaign_orchestration_detail", campaign_id=campaign_id,
                            orchestration_error=error, orchestration_notice=notice))


@app.post("/curator/growth/orchestration/<orchestration_id>/items/<work_item_id>/advance")
def knowledge_campaign_orchestration_advance(orchestration_id, work_item_id):
    service = KnowledgeCampaignOrchestrationService()
    campaign_id = request.form.get("campaign_id", "")
    try:
        orchestration = service.advance_item(orchestration_id, work_item_id)
        campaign_id = orchestration["campaign_id"]
        outcomes = orchestration.get("execution", {}).get("outcomes", [])
        outcome = outcomes[0] if outcomes else {}
        if outcome.get("status") not in {"completed", "package_reused"}:
            error = outcome.get("message") or "The campaign item could not be advanced."
            notice = ""
        elif outcome.get("action") == "prepare_evidence":
            disposition = outcome.get("package_disposition", "created")
            notice = (
                f"Evidence package {outcome.get('extraction_id') or 'unknown'} {disposition} "
                f"for source {outcome.get('source_candidate_id') or 'unknown'}."
            )
            error = ""
        else:
            notice = "Campaign work item advanced one step."
            error = ""
    except KnowledgeCampaignOrchestrationError as exception:
        error = str(exception)
        notice = ""
    return redirect(url_for("knowledge_campaign_orchestration_detail", campaign_id=campaign_id,
                            orchestration_error=error, orchestration_notice=notice))


@app.post("/curator/growth/coverage-campaigns/<campaign_id>/workflow-generation")
def knowledge_workflow_generation_prepare(campaign_id):
    try:
        package = KnowledgeWorkflowGenerationService().prepare(
            campaign_id, request.form.get("work_item_id", ""), request.form.get("intent", "")
        )
    except KnowledgeWorkflowGenerationError as exception:
        return redirect(url_for("knowledge_coverage_campaign_detail", campaign_id=campaign_id,
                                workflow_error=str(exception)))
    return redirect(url_for("knowledge_workflow_generation_detail", generation_id=package["generation_id"]))


@app.get("/curator/growth/workflow-generation/<generation_id>")
def knowledge_workflow_generation_detail(generation_id):
    try:
        package = KnowledgeWorkflowGenerationService().get(generation_id)
    except KnowledgeWorkflowGenerationError:
        abort(404)
    return render_template("knowledge_workflow_generation_detail.html", package=package,
                           workflow_error=request.args.get("workflow_error", ""))


@app.post("/curator/growth/workflow-generation/<generation_id>/plan")
def knowledge_workflow_generation_plan(generation_id):
    try:
        KnowledgeWorkflowGenerationService().plan(generation_id)
        error = ""
    except KnowledgeWorkflowGenerationError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_workflow_generation_detail", generation_id=generation_id,
                            workflow_error=error))


@app.post("/curator/growth/workflow-generation/<generation_id>/draft")
def knowledge_workflow_generation_draft(generation_id):
    try:
        KnowledgeWorkflowGenerationService().prepare_draft(generation_id)
        error = ""
    except KnowledgeWorkflowGenerationError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_workflow_generation_detail", generation_id=generation_id,
                            workflow_error=error))


@app.post("/curator/growth/workflow-generation/<generation_id>/review")
def knowledge_workflow_generation_review(generation_id):
    try:
        KnowledgeWorkflowGenerationService().review(
            generation_id, request.form.get("decision", ""), request.form.get("notes", "")
        )
        error = ""
    except KnowledgeWorkflowGenerationError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_workflow_generation_detail", generation_id=generation_id,
                            workflow_error=error))


@app.post("/curator/growth/workflow-generation/<generation_id>/handoff")
def knowledge_workflow_generation_handoff(generation_id):
    try:
        package = KnowledgeWorkflowGenerationService().handoff(generation_id)
    except KnowledgeWorkflowGenerationError as exception:
        return redirect(url_for("knowledge_workflow_generation_detail", generation_id=generation_id,
                                workflow_error=str(exception)))
    return redirect(url_for("workflow_editor", filename=package["content_studio_filename"]))


@app.post("/curator/growth/coverage-campaigns/<campaign_id>/analyze")
def knowledge_coverage_campaign_analyze(campaign_id):
    try:
        KnowledgeCoveragePlannerService().analyze(campaign_id)
    except KnowledgeCoveragePlannerError:
        abort(404)
    return redirect(url_for("knowledge_coverage_campaign_detail", campaign_id=campaign_id))


@app.post("/curator/growth/coverage-campaigns/<campaign_id>/research")
def knowledge_source_research_prepare(campaign_id):
    try:
        package = KnowledgeSourceResearchService().create(
            campaign_id, request.form.get("gap_id", ""), request.form.get("work_item_id", ""),
        )
    except KnowledgeSourceResearchError as exception:
        return redirect(url_for("knowledge_coverage_campaign_detail", campaign_id=campaign_id,
                                research_error=str(exception)))
    return redirect(url_for("knowledge_source_research_detail", package_id=package["package_id"]))


@app.get("/curator/growth/source-research/<package_id>")
def knowledge_source_research_detail(package_id):
    try:
        package = KnowledgeSourceResearchService().get(package_id)
    except KnowledgeSourceResearchError:
        abort(404)
    extraction_packages = KnowledgeEvidenceExtractionService().list_for_research(package_id)
    extraction_by_source = {item["source_candidate_id"]: item for item in extraction_packages}
    return render_template("knowledge_source_research_detail.html", package=package,
                           extraction_by_source=extraction_by_source,
                           research_error=request.args.get("research_error", ""))


@app.post("/curator/growth/source-research/<package_id>/run")
def knowledge_source_research_run(package_id):
    service = KnowledgeSourceResearchService()
    try:
        service.run(package_id, force_external=request.form.get("force_external") == "true")
        error = ""
    except KnowledgeSourceResearchError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_source_research_detail", package_id=package_id,
                            research_error=error))


@app.post("/curator/growth/source-research/<package_id>/candidates/<candidate_id>")
def knowledge_source_candidate_review(package_id, candidate_id):
    try:
        KnowledgeSourceResearchService().set_candidate_state(
            package_id, candidate_id, request.form.get("state", ""), request.form.get("notes", ""),
        )
    except KnowledgeSourceResearchError as exception:
        return redirect(url_for("knowledge_source_research_detail", package_id=package_id,
                                research_error=str(exception)))
    return redirect(url_for("knowledge_source_research_detail", package_id=package_id))


@app.post("/curator/growth/source-research/<package_id>/candidates/<candidate_id>/refresh")
def knowledge_source_candidate_refresh(package_id, candidate_id):
    try:
        KnowledgeSourceResearchService().refresh_candidate(package_id, candidate_id)
        error = ""
    except KnowledgeSourceResearchError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_source_research_detail", package_id=package_id,
                            research_error=error))


@app.post("/curator/growth/source-research/<package_id>/review")
def knowledge_source_package_review(package_id):
    try:
        KnowledgeSourceResearchService().review(
            package_id, request.form.get("status", ""), request.form.get("notes", ""),
        )
        error = ""
    except KnowledgeSourceResearchError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_source_research_detail", package_id=package_id,
                            research_error=error))


@app.post("/curator/growth/source-research/<package_id>/candidates/<candidate_id>/extract")
def knowledge_evidence_extraction_prepare(package_id, candidate_id):
    try:
        package = KnowledgeEvidenceExtractionService().prepare(package_id, candidate_id)
    except KnowledgeEvidenceExtractionError as exception:
        return redirect(url_for("knowledge_source_research_detail", package_id=package_id,
                                research_error=str(exception)))
    return redirect(url_for("knowledge_evidence_extraction_detail",
                            extraction_id=package["extraction_id"]))


@app.get("/curator/growth/evidence-extraction/<extraction_id>")
def knowledge_evidence_extraction_detail(extraction_id):
    try:
        service = KnowledgeEvidenceExtractionService()
        workspace = service.review_workspace(
            extraction_id, review_state=request.args.get("review_state", "all"),
            evidence_type=request.args.get("evidence_type", "all"),
            assistance=request.args.get("assistance", "all"),
            machine_recommendation=request.args.get("machine_recommendation", "all"),
            human_role=request.args.get("human_role", "all"),
        )
        package = workspace["package"]
        reextraction = service.reextraction_state(package)
    except KnowledgeEvidenceExtractionError:
        abort(404)
    continuation_available = bool(
        workspace["complete"] and
        KnowledgeClaimPlanningService().workflow_is_eligible(
            str(package.get("campaign_id") or ""), str(package.get("work_item_id") or "")
        )
    )
    return render_template("knowledge_evidence_extraction_detail.html", package=package,
                           workspace=workspace,
                           reextraction=reextraction,
                           continuation_available=continuation_available,
                           extraction_error=request.args.get("extraction_error", ""),
                           extraction_notice=request.args.get("extraction_notice", ""))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/run")
def knowledge_evidence_extraction_run(extraction_id):
    service = KnowledgeEvidenceExtractionService()
    try:
        package = service.get(extraction_id)
        if package.get("status") != "proposed":
            error = ("Initial extraction has already run. Use the governed re-extraction "
                     "action when an updated extractor or stale source makes it available.")
        else:
            service.extract(extraction_id)
            error = ""
    except KnowledgeEvidenceExtractionError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_evidence_extraction_detail", extraction_id=extraction_id,
                            extraction_error=error))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/reextract")
def knowledge_evidence_reextract(extraction_id):
    service = KnowledgeEvidenceExtractionService()
    try:
        before = service.reextraction_state(extraction_id)
        service.reextract(extraction_id)
        error = ""
        notice = ("Evidence was re-extracted with the current deterministic rules. "
                  "All active evidence requires human review."
                  if before["available"] else
                  "This package already uses the current extraction rules; no changes were made.")
    except KnowledgeEvidenceExtractionError as exception:
        error, notice = str(exception), ""
    return redirect(url_for("knowledge_evidence_extraction_detail", extraction_id=extraction_id,
                            extraction_error=error, extraction_notice=notice))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/refresh")
def knowledge_evidence_extraction_refresh(extraction_id):
    service = KnowledgeEvidenceExtractionService()
    try:
        service.refresh_status(extraction_id)
        error = ""
    except KnowledgeEvidenceExtractionError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_evidence_extraction_detail", extraction_id=extraction_id,
                            extraction_error=error))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/evidence/<evidence_id>")
def knowledge_evidence_review(extraction_id, evidence_id):
    service = KnowledgeEvidenceExtractionService()
    requested_filters = {
        "review_state": request.form.get("review_state", "all"),
        "evidence_type": request.form.get("evidence_type", "all"),
        "assistance": request.form.get("assistance", "all"),
        "machine_recommendation": request.form.get("machine_recommendation", "all"),
        "human_role": request.form.get("human_role", "all"),
    }
    try:
        service.review_evidence(
            extraction_id, evidence_id, request.form.get("decision", ""),
            request.form.get("notes", ""),
        )
        error = ""
    except KnowledgeEvidenceExtractionError as exception:
        error = str(exception)
    try:
        workspace = service.review_workspace(extraction_id, **requested_filters)
        preserved_filters = {
            "review_state": workspace["review_state"],
            "evidence_type": workspace["evidence_type"],
            "assistance": workspace["assistance"],
            "machine_recommendation": workspace["machine_recommendation"],
            "human_role": workspace["human_role"],
        }
        next_undecided = workspace["next_undecided"]
    except KnowledgeEvidenceExtractionError:
        preserved_filters = {"review_state": "all", "evidence_type": "all",
                             "assistance": "all", "machine_recommendation": "all",
                             "human_role": "all"}
        next_undecided = None
    target = url_for(
        "knowledge_evidence_extraction_detail", extraction_id=extraction_id,
        extraction_error=error, **preserved_filters,
    )
    if next_undecided:
        target += f"#evidence-{next_undecided}"
    return redirect(target)


@app.post("/curator/growth/evidence-extraction/<extraction_id>/evidence/<evidence_id>/candidacy")
def knowledge_evidence_candidacy(extraction_id, evidence_id):
    service = KnowledgeEvidenceExtractionService()
    requested_filters = {
        "review_state": request.form.get("review_state", "all"),
        "evidence_type": request.form.get("evidence_type", "all"),
        "assistance": request.form.get("assistance", "all"),
        "machine_recommendation": request.form.get("machine_recommendation", "all"),
        "human_role": request.form.get("human_role", "all"),
    }
    try:
        service.set_candidacy_role(extraction_id, evidence_id, request.form.get("role", ""))
        error = ""
    except KnowledgeEvidenceExtractionError as exception:
        error = str(exception)
    try:
        workspace = service.review_workspace(extraction_id, **requested_filters)
        preserved_filters = {
            "review_state": workspace["review_state"],
            "evidence_type": workspace["evidence_type"],
            "assistance": workspace["assistance"],
            "machine_recommendation": workspace["machine_recommendation"],
            "human_role": workspace["human_role"],
        }
        next_matching = workspace["next_matching"]
    except KnowledgeEvidenceExtractionError:
        preserved_filters = {"review_state": "all", "evidence_type": "all",
                             "assistance": "all", "machine_recommendation": "all",
                             "human_role": "all"}
        next_matching = None
    target = url_for("knowledge_evidence_extraction_detail", extraction_id=extraction_id,
                     extraction_error=error, **preserved_filters)
    if next_matching:
        target += f"#evidence-{next_matching}"
    return redirect(target)


@app.post("/curator/growth/evidence-extraction/<extraction_id>/candidacy/confirm")
def knowledge_evidence_candidacy_confirm(extraction_id):
    try:
        KnowledgeEvidenceExtractionService().confirm_candidate_set(extraction_id)
        error = ""
    except KnowledgeEvidenceExtractionError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_evidence_extraction_detail", extraction_id=extraction_id,
                            extraction_error=error))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/candidacy/bulk-context")
def knowledge_evidence_candidacy_bulk_context(extraction_id):
    service = KnowledgeEvidenceExtractionService()
    requested_filters = {
        "review_state": request.form.get("review_state", "all"),
        "evidence_type": request.form.get("evidence_type", "all"),
        "assistance": request.form.get("assistance", "all"),
        "machine_recommendation": request.form.get("machine_recommendation", "all"),
        "human_role": request.form.get("human_role", "all"),
    }
    try:
        expected_count = int(request.form.get("expected_count", ""))
        service.bulk_assign_visible_machine_context(
            extraction_id, expected_count=expected_count, **requested_filters,
        )
        error = ""
        notice = (
            f"Assigned {expected_count} visible machine-Context unit"
            f"{'s' if expected_count != 1 else ''} as Reviewer Context."
        )
    except (KnowledgeEvidenceExtractionError, TypeError, ValueError) as exception:
        error, notice = str(exception), ""
    return redirect(url_for(
        "knowledge_evidence_extraction_detail", extraction_id=extraction_id,
        extraction_error=error, extraction_notice=notice, **requested_filters,
    ))


@app.post("/curator/growth/evidence-extraction/<extraction_id>/candidacy/bulk-context-all")
def knowledge_evidence_candidacy_bulk_context_all(extraction_id):
    service = KnowledgeEvidenceExtractionService()
    try:
        expected_count = int(request.form.get("expected_count", ""))
        service.bulk_assign_all_machine_context(
            extraction_id, expected_count=expected_count,
        )
        error = ""
        notice = (
            f"Assigned {expected_count} machine-Context unit"
            f"{'s' if expected_count != 1 else ''} as Reviewer Context."
        )
    except (KnowledgeEvidenceExtractionError, TypeError, ValueError) as exception:
        error, notice = str(exception), ""
    return redirect(url_for(
        "knowledge_evidence_extraction_detail", extraction_id=extraction_id,
        extraction_error=error, extraction_notice=notice,
    ))


@app.post("/curator/growth/coverage-campaigns/<campaign_id>/draft-generation")
def knowledge_draft_generation_prepare(campaign_id):
    try:
        package = KnowledgeDraftGenerationService().prepare(
            campaign_id, request.form.get("gap_id", ""), request.form.get("work_item_id", ""),
            request.form.get("notes", ""),
        )
    except KnowledgeDraftGenerationError as exception:
        return redirect(url_for("knowledge_coverage_campaign_detail", campaign_id=campaign_id,
                                draft_error=str(exception)))
    return redirect(url_for("knowledge_draft_generation_detail", package_id=package["package_id"]))


@app.get("/curator/growth/draft-generation/<package_id>")
def knowledge_draft_generation_detail(package_id):
    try:
        package = KnowledgeDraftGenerationService().get(package_id)
    except KnowledgeDraftGenerationError:
        abort(404)
    claim_planning = KnowledgeClaimPlanningService()
    claim_plans = claim_planning.list_for_kdg(package_id)
    assemblies = KnowledgeDraftAssemblyService().list_for_kdg(package_id)
    return render_template("knowledge_draft_generation_detail.html", package=package,
                           claim_plans=claim_plans,
                           assemblies=assemblies,
                           claim_planning_eligible=claim_planning.is_eligible(package_id),
                           draft_error=request.args.get("draft_error", ""))


@app.post("/curator/growth/draft-generation/<package_id>/claim-planning")
def knowledge_claim_planning_prepare(package_id):
    try:
        plan = KnowledgeClaimPlanningService().prepare(package_id)
    except KnowledgeClaimPlanningError as exception:
        return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id,
                                draft_error=str(exception)))
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan["claim_plan_id"]))


@app.post("/curator/growth/coverage-campaigns/<campaign_id>/work-items/<work_item_id>/workflow-claim-planning")
def knowledge_workflow_claim_planning_prepare(campaign_id, work_item_id):
    """Human-initiated entry into the existing supervised Phase 6 workspace."""
    try:
        plan = KnowledgeClaimPlanningService().prepare_workflow(campaign_id, work_item_id)
    except KnowledgeClaimPlanningError as exception:
        return redirect(url_for("knowledge_coverage_campaign_detail",
                                campaign_id=campaign_id, claim_error=str(exception)))
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan["claim_plan_id"]))


@app.get("/curator/growth/claim-planning/<plan_id>")
def knowledge_claim_planning_detail(plan_id):
    try:
        plan = KnowledgeClaimPlanningService().get(plan_id)
    except KnowledgeClaimPlanningError:
        abort(404)
    return render_template("knowledge_claim_planning_detail.html", plan=plan,
                           planning_error=request.args.get("planning_error", ""))


@app.post("/curator/growth/claim-planning/<plan_id>/run")
def knowledge_claim_planning_run(plan_id):
    try:
        KnowledgeClaimPlanningService().plan(plan_id)
        error = ""
    except KnowledgeClaimPlanningError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                            planning_error=error))


@app.post("/curator/growth/claim-planning/<plan_id>/claims/<claim_id>")
def knowledge_claim_review(plan_id, claim_id):
    try:
        KnowledgeClaimPlanningService().review_claim(
            plan_id, claim_id, request.form.get("decision", ""), request.form.get("notes", "")
        )
        error = ""
    except KnowledgeClaimPlanningError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                            planning_error=error))


@app.post("/curator/growth/claim-planning/<plan_id>/sections/<section_name>")
def knowledge_claim_section_review(plan_id, section_name):
    try:
        KnowledgeClaimPlanningService().review_section(
            plan_id, section_name, request.form.get("decision", ""), request.form.get("notes", "")
        )
        error = ""
    except KnowledgeClaimPlanningError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                            planning_error=error))


@app.post("/curator/growth/claim-planning/<plan_id>/conflicts/<conflict_id>")
def knowledge_claim_conflict_review(plan_id, conflict_id):
    try:
        KnowledgeClaimPlanningService().review_conflict(
            plan_id, conflict_id, request.form.get("decision", ""), request.form.get("notes", "")
        )
        error = ""
    except KnowledgeClaimPlanningError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                            planning_error=error))


@app.post("/curator/growth/claim-planning/<plan_id>/apply")
def knowledge_claim_planning_apply(plan_id):
    try:
        plan = KnowledgeClaimPlanningService().get(plan_id)
        if plan.get("status") != "ready_for_drafting":
            raise KnowledgeClaimPlanningError(
                "Resolve required gaps and review every planned claim before drafting."
            )
        KnowledgeDraftGenerationService().refresh_from_approved_claim_plan(plan["kdg_package_id"])
    except (KnowledgeClaimPlanningError, KnowledgeDraftGenerationError) as exception:
        return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                                planning_error=str(exception)))
    return redirect(url_for("knowledge_draft_generation_detail", package_id=plan["kdg_package_id"]))


@app.post("/curator/growth/claim-planning/<plan_id>/assemble")
def knowledge_draft_assembly_create(plan_id):
    try:
        assembly = KnowledgeDraftAssemblyService().assemble(plan_id)
    except KnowledgeDraftAssemblyError as exception:
        return redirect(url_for("knowledge_claim_planning_detail", plan_id=plan_id,
                                planning_error=str(exception)))
    return redirect(url_for("knowledge_draft_assembly_detail",
                            assembly_id=assembly["assembly_id"]))


@app.get("/curator/growth/draft-assembly/<assembly_id>")
def knowledge_draft_assembly_detail(assembly_id):
    try:
        assembly = KnowledgeDraftAssemblyService().get(assembly_id)
    except KnowledgeDraftAssemblyError:
        abort(404)
    return render_template("knowledge_draft_assembly_detail.html", assembly=assembly,
                           assembly_error=request.args.get("assembly_error", ""))


@app.post("/curator/growth/draft-assembly/<assembly_id>/reassemble")
def knowledge_draft_assembly_reassemble(assembly_id):
    service = KnowledgeDraftAssemblyService()
    try:
        assembly = service.get(assembly_id)
        service.assemble(assembly["claim_plan_id"])
        error = ""
    except KnowledgeDraftAssemblyError as exception:
        error = str(exception)
    return redirect(url_for("knowledge_draft_assembly_detail", assembly_id=assembly_id,
                            assembly_error=error))


@app.post("/curator/growth/draft-assembly/<assembly_id>/handoff")
def knowledge_draft_assembly_handoff(assembly_id):
    try:
        assembly = KnowledgeDraftAssemblyService().handoff(assembly_id)
    except KnowledgeDraftAssemblyError as exception:
        return redirect(url_for("knowledge_draft_assembly_detail", assembly_id=assembly_id,
                                assembly_error=str(exception)))
    return redirect(url_for("review_draft", article_id=assembly["content_studio_article_id"]))


@app.post("/curator/growth/draft-generation/<package_id>/refine")
def knowledge_draft_generation_refine(package_id):
    try:
        KnowledgeDraftRefinementService().refine(package_id)
    except (KnowledgeDraftRefinementError, KnowledgeDraftGenerationError) as exception:
        return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id,
                                draft_error=str(exception)))
    return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id))


@app.post("/curator/growth/draft-generation/<package_id>/handoff")
def knowledge_draft_generation_handoff(package_id):
    service = KnowledgeDraftGenerationService()
    try:
        package = service.accept_into_content_studio(package_id)
    except KnowledgeDraftGenerationError as exception:
        return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id,
                                draft_error=str(exception)))
    if package.get("generation_status") == "accepted_into_content_studio":
        return redirect(url_for("review_draft", article_id=package["content_studio_article_id"]))
    return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id))


@app.post("/curator/growth/draft-generation/<package_id>/reject")
def knowledge_draft_generation_reject(package_id):
    try:
        KnowledgeDraftGenerationService().reject(package_id, request.form.get("notes", ""))
    except KnowledgeDraftGenerationError as exception:
        return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id,
                                draft_error=str(exception)))
    return redirect(url_for("knowledge_draft_generation_detail", package_id=package_id))


@app.route("/curator/tasks/<task_id>")
def curator_task_detail(task_id):
    from app.services.curator_structural_repair_review_service import (
        CuratorStructuralRepairReviewService,
    )

    repository_root = _structural_repository_root()
    task_navigation = CuratorTaskNavigationService.resolve(
        request.args.get("origin", ""), request.args.get("return_to", ""), task_id=task_id
    )
    return_to = task_navigation.return_url
    origin = task_navigation.origin
    session_id = request.args.get("curator_session", "")
    category = request.args.get("category", "all")
    try:
        task = CuratorTaskService(repository_root).get(
            task_id, session_id=session_id,
            return_to=return_to, category=category, origin=origin,
        )
        session_task_actionable = (
            CuratorFixSessionService(repository_root).task_action_eligible(session_id, task_id)
            if session_id else False
        )
    except (CuratorMemoryError, CuratorFixSessionError):
        abort(404)
    automation_policy = None
    if (
        task.get("curator_rule") == "CUR-WR-PROGRESS"
        and task.get("finding_type") == "workflow_reasoning_progress_inconsistency"
    ):
        automation_policy = CuratorProgressAutoRepairPolicyService(
            repository_root
        ).evaluate(task_id).to_dict()
        gates = automation_policy.get("gate_results", ())
        automation_policy["passed_gate_count"] = sum(
            bool(gate.get("passed")) for gate in gates
        )
        automation_policy["failed_gate_count"] = sum(
            not bool(gate.get("passed")) for gate in gates
        )
    messages = {
        "updated": ("success", "Knowledge Task updated."),
        "invalid": ("danger", request.args.get("error") or "The requested task change could not be applied."),
        "resolved": ("success", "Task resolved and the maintenance session was reconciled. It is safe to return to the Fix Wizard."),
        "prepared": ("success", "Assisted Resolution Package prepared for human review."),
        "draft_created": ("success", "Article draft created. No workflow relationship was changed."),
        "verified": ("success", "Current affected content was checked. Review the verification result before deciding whether to resolve."),
        "proposal_prepared": ("success", "Help-text proposal prepared without changing the workflow."),
        "proposal_updated": ("success", "Proposed help text updated."),
        "proposal_approved": ("success", "The human approval was recorded. The workflow remains unchanged."),
        "publication_recorded": ("success", "The approved change was associated with its published workflow version."),
        "outcome_measured": ("success", "Runtime outcome evidence was measured."),
        "relationship_applied": ("success", "The approved relationship metadata repair was applied and verified. The task remains open for human resolution."),
        "relationship_apply_failed": ("danger", request.args.get("error") or "The relationship repair was not applied."),
    }
    kind, message = messages.get(request.args.get("status", ""), ("info", ""))
    return render_template(
        "curator_task_detail.html", task=task,
        owners=CuratorTaskService.OWNERS, priorities=CuratorTaskService.PRIORITIES,
        status_kind=kind, status_message=message,
        resolution_package=CuratorResolutionService(repository_root).get(task_id),
        confusing_step_proposal=CuratorConfusingStepImprovementService(repository_root).get(task_id),
        verification_presentation=CuratorVerificationPresentationService.present(
            task.get("current_verification") if isinstance(task, dict) else task.current_verification
        ),
        task_review=CuratorTaskReviewPresentationService.present(task),
        structural_repair_state=CuratorStructuralRepairReviewService(
            repository_root
        ).applied_state(task_id),
        automation_policy=automation_policy,
        session_task_actionable=session_task_actionable,
        return_to=return_to, curator_session=session_id, category=category,
        task_navigation=task_navigation, task_origin=origin,
        task_return_context=CuratorTaskNavigationService.task_return(
            task_id, task_navigation, session_id=session_id, category=category,
        ),
        previous_task_return_context=CuratorTaskNavigationService.previous_task_return(
            task_id, task_navigation, session_id=session_id, category=category,
        ),
    )


@app.post("/curator/tasks/<task_id>/relationship-proposal/apply")
def curator_relationship_proposal_apply(task_id):
    navigation = CuratorTaskNavigationService.resolve(
        request.form.get("origin", ""), request.form.get("return_to", ""), task_id=task_id
    )
    try:
        CuratorRelationshipRepairApplicationService().apply(
            task_id,
            approval_token=request.form.get("approval_token", ""),
            approved=request.form.get("approved") == "yes",
        )
        status, error = "relationship_applied", ""
    except (CuratorRelationshipRepairApplicationError, CuratorMemoryError, OSError,
            UnicodeDecodeError, ValueError, json.JSONDecodeError) as exception:
        status, error = "relationship_apply_failed", str(exception)
    return redirect(url_for(
        "curator_task_detail", task_id=task_id, status=status, error=error,
        origin=navigation.origin, return_to=navigation.return_url,
    ))


@app.get("/__dev/phase3-relationship-harness/tasks/<task_id>")
def phase3_relationship_harness_task(task_id):
    harness = _require_phase3_harness()
    try:
        task = CuratorTaskService(harness.root).get(task_id)
    except CuratorMemoryError:
        abort(404)
    status = request.args.get("status", "")
    return render_template(
        "curator_task_detail.html", task=task,
        owners=CuratorTaskService.OWNERS, priorities=CuratorTaskService.PRIORITIES,
        status_kind="success" if status == "relationship_applied" else "danger" if status else "info",
        status_message=("The temporary fixture repair was applied and verified. The fixture task remains open."
                        if status == "relationship_applied" else
                        ("The request was not applied. " + request.args.get("error", "") if status else "")),
        resolution_package=None, confusing_step_proposal=None,
        verification_presentation=CuratorVerificationPresentationService.present(
            task.get("current_verification")
        ),
        task_review=CuratorTaskReviewPresentationService.present(task),
        session_task_actionable=False,
        return_to=url_for("phase3_relationship_harness_queue"),
        curator_session="", category="all", harness_mode=True,
        relationship_apply_url=url_for("phase3_relationship_harness_apply", task_id=task_id),
        verification_url=url_for("phase3_relationship_harness_verify", task_id=task_id),
    )


@app.post("/__dev/phase3-relationship-harness/tasks/<task_id>/apply")
def phase3_relationship_harness_apply(task_id):
    harness = _require_phase3_harness()
    try:
        CuratorRelationshipRepairApplicationService(harness.root).apply(
            task_id, approval_token=request.form.get("approval_token", ""),
            approved=request.form.get("approved") == "yes",
        )
        status, error = "relationship_applied", ""
    except (CuratorRelationshipRepairApplicationError, CuratorMemoryError, OSError,
            UnicodeDecodeError, ValueError, json.JSONDecodeError) as exception:
        status, error = "relationship_apply_failed", str(exception)
    return redirect(url_for("phase3_relationship_harness_task", task_id=task_id,
                            status=status, error=error))


@app.post("/__dev/phase3-relationship-harness/tasks/<task_id>/verify")
def phase3_relationship_harness_verify(task_id):
    harness = _require_phase3_harness()
    try:
        CuratorTargetedVerificationService(harness.root).verify(task_id)
        error = ""
    except CuratorMemoryError as exception:
        error = str(exception)
    return redirect(url_for("phase3_relationship_harness_task", task_id=task_id, error=error))


@app.post("/curator/tasks/<task_id>/confusing-step-improvement")
def curator_confusing_step_improvement(task_id):
    action = request.form.get("action", "")
    service = CuratorConfusingStepImprovementService()
    status, error = "proposal_updated", ""
    try:
        if action == "prepare":
            service.prepare(task_id)
            status = "proposal_prepared"
        elif action == "edit":
            service.edit(task_id, request.form.get("proposed_help_text", ""))
        elif action == "approve":
            service.edit(task_id, request.form.get("proposed_help_text", ""))
            service.approve(
                task_id,
                reviewer=request.form.get("reviewer", ""),
                note=request.form.get("approval_note", ""),
            )
            status = "proposal_approved"
        elif action == "record_published":
            service.record_published_version(task_id)
            status = "publication_recorded"
        elif action == "measure":
            service.measure(task_id)
            status = "outcome_measured"
        else:
            raise CuratorConfusingStepImprovementError("Unsupported improvement action.")
    except (CuratorConfusingStepImprovementError, WorkflowPublicationError) as exception:
        status, error = "invalid", str(exception)
    navigation = CuratorTaskNavigationService.resolve(
        request.form.get("origin", ""), request.form.get("return_to", ""), task_id=task_id
    )
    return redirect(url_for("curator_task_detail", task_id=task_id, status=status, error=error,
                            origin=navigation.origin, return_to=navigation.return_url))


@app.get("/curator/tasks/<task_id>/confusing-step-improvement/handoff")
def curator_confusing_step_improvement_handoff(task_id):
    navigation = CuratorTaskNavigationService.resolve(
        request.args.get("origin", ""), request.args.get("return_to", ""), task_id=task_id
    )
    try:
        target = CuratorConfusingStepImprovementService().handoff(task_id)
    except CuratorConfusingStepImprovementError as exception:
        return redirect(url_for(
            "curator_task_detail", task_id=task_id, status="invalid", error=str(exception)
        ))
    return_to = url_for("curator_task_detail", task_id=task_id, origin=navigation.origin,
                        return_to=navigation.return_url, _external=False)
    return redirect(url_for(
        "workflow_editor", filename=target["filename"], node=target["node_id"],
        curator_task=task_id, curator_return=return_to,
    ))


@app.post("/curator/tasks/<task_id>/verify")
def curator_task_verify(task_id):
    navigation = CuratorTaskNavigationService.resolve(
        request.form.get("origin", ""), request.form.get("return_to", ""), task_id=task_id
    )
    session_id = request.form.get("curator_session", "")
    category = request.form.get("category", "all")
    try:
        CuratorTargetedVerificationService().verify(task_id)
        status, error = "verified", ""
    except CuratorMemoryError as exception:
        status, error = "invalid", str(exception)
    return redirect(url_for("curator_task_detail", task_id=task_id, status=status,
                            error=error, origin=navigation.origin, return_to=navigation.return_url,
                            curator_session=session_id, category=category))


@app.post("/curator/tasks/<task_id>/actions")
def curator_task_action(task_id):
    navigation = CuratorTaskNavigationService.resolve(
        request.form.get("origin", ""), request.form.get("return_to", ""), task_id=task_id
    )
    return_to = navigation.return_url
    session_id = request.form.get("curator_session", "")
    action = request.form.get("action", "")
    category = request.form.get("category", "all")
    resolve_continue = action == "resolve_continue"
    service_action = "resolve" if resolve_continue else action
    try:
        session_service = None
        if service_action == "defer" and session_id:
            session_service = CuratorFixSessionService()
            if not session_service.task_action_eligible(session_id, task_id):
                raise CuratorFixSessionError(
                    "This task is not an actionable item in the current maintenance session."
                )
        CuratorTaskService().update(
            task_id,
            action=service_action,
            owner=request.form.get("owner", ""),
            priority=request.form.get("priority", ""),
            note=request.form.get("note", ""),
            session_id=session_id,
            expected_fingerprint=request.form.get("affected_fingerprint", ""),
        )
        status = "updated"
        if service_action == "defer" and session_id:
            CuratorSessionReconciliationService().reconcile(session_id, trigger="task_deferral")
            if return_to.startswith("/curator/fix/"):
                separator = "&" if "?" in return_to else "?"
                return redirect(f"{return_to}{separator}status=deferred")
        if service_action == "resolve" and session_id:
            reconciled = CuratorSessionReconciliationService().reconcile(session_id, trigger="task_resolution")
            status = "resolved"
            if resolve_continue:
                return redirect(url_for("curator_fix_session", session_id=session_id,
                                        category=category, status="task_resolved",
                                        repaired_task=task_id,
                                        debt=reconciled.get("session_debt_reduced", 0)))
    except (CuratorMemoryError, CuratorFixSessionError) as error:
        status = "invalid"
        app.logger.warning(json.dumps({"event": "curator_task_change_rejected", "request_id": g.request_id,
                                       "task_id": task_id, "action": action,
                                       "error_type": type(error).__name__}))
        return redirect(url_for("curator_task_detail", task_id=task_id, status=status,
                                error=str(error), origin=navigation.origin, return_to=return_to,
                                curator_session=session_id, category=category))
    return redirect(url_for("curator_task_detail", task_id=task_id, status=status,
                            origin=navigation.origin, return_to=return_to,
                            curator_session=session_id, category=category))


@app.post("/curator/tasks/<task_id>/assisted-resolution")
def curator_assisted_resolution(task_id):
    action = request.form.get("action", "")
    navigation = CuratorTaskNavigationService.resolve(
        request.form.get("origin", ""), request.form.get("return_to", ""), task_id=task_id
    )
    try:
        service = CuratorResolutionService()
        if action == "prepare":
            service.prepare(task_id)
            status = "prepared"
        elif action == "create_draft":
            service.create_article_draft(task_id, confirmed=request.form.get("confirmed") == "yes")
            return redirect(url_for("curator_assisted_resolution_article", task_id=task_id,
                                    origin=navigation.origin, return_to=navigation.return_url))
        else:
            raise ResolutionPackageError("Unsupported assisted-resolution action.")
    except (ResolutionPackageError, ValueError, KnowledgeRepositoryError):
        status = "invalid"
    return redirect(url_for("curator_task_detail", task_id=task_id, status=status,
                            origin=navigation.origin, return_to=navigation.return_url))


@app.get("/curator/tasks/<task_id>/assisted-resolution/article")
def curator_assisted_resolution_article(task_id):
    navigation = CuratorTaskNavigationService.resolve(
        request.args.get("origin", ""), request.args.get("return_to", ""), task_id=task_id
    )
    return_to = (
        navigation.return_url
        if navigation.origin == "maintenance"
        else CuratorTaskNavigationService.assisted_task_return(task_id, navigation)
    )
    try:
        state, article = CuratorResolutionService().article_location(task_id)
    except ResolutionPackageError:
        abort(404)
    endpoint = "review_draft" if state == "draft" else "view_published"
    return redirect(url_for(endpoint, article_id=article["id"], return_to=return_to))


@app.post("/curator/assisted-resolution/first-batch")
def curator_assisted_resolution_batch():
    try:
        CuratorBatchService().prepare_first_batch()
        status = "batch_completed"
    except ResolutionPackageError:
        status = "batch_failed"
    return redirect(url_for("curator_dashboard", status=status))


def _structural_repository_root() -> Path:
    configured = app.config.get("STRUCTURAL_REPAIR_REPOSITORY_ROOT")
    return Path(configured).resolve() if configured else Path(app.root_path).parent.resolve()


def _structural_csrf_token() -> str:
    token = session.get("structural_repair_csrf")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["structural_repair_csrf"] = token
    return token


def _require_structural_csrf() -> None:
    supplied = str(request.form.get("csrf_token") or "")
    expected = str(session.get("structural_repair_csrf") or "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400)


def _remember_structural_preview(task_id: str, fix_session_id: str, preview_token: str) -> None:
    reviewed = dict(session.get("structural_repair_previews") or {})
    reviewed[f"{fix_session_id}:{task_id}"] = str(preview_token or "")
    session["structural_repair_previews"] = reviewed


def _require_reviewed_structural_preview(task_id: str, fix_session_id: str,
                                         preview_token: str) -> None:
    reviewed = session.get("structural_repair_previews") or {}
    expected = str(reviewed.get(f"{fix_session_id}:{task_id}") or "")
    if not expected or not secrets.compare_digest(expected, str(preview_token or "")):
        raise ValueError("The reviewed structural preview is stale.")


def _structural_navigation(task_id: str, fix_session_id: str, item_id: str,
                           origin: str, return_to: str):
    navigation = CuratorTaskNavigationService.resolve(origin, return_to, task_id=task_id)
    expected_prefix = f"/curator/fix/{fix_session_id}"
    if navigation.origin != "maintenance" or not (
            navigation.return_url == expected_prefix
            or navigation.return_url.startswith(expected_prefix + "?")):
        navigation = CuratorTaskNavigationService.resolve(
            "maintenance",
            url_for("curator_fix_session", session_id=fix_session_id, item=item_id),
            task_id=task_id,
        )
    return navigation


def _structural_failure(code: str) -> dict[str, str | bool]:
    messages = {
        "approval_missing": "This repair approval is unavailable. Generate and review a new preview.",
        "approval_invalid": "This repair can no longer be applied from the preview you approved. Generate and review a new preview.",
        "approval_expired": "This repair approval expired. Generate and review a new preview.",
        "preview_unknown": "The governed preview or evidence specification changed. Generate and review a new preview.",
        "plan_invalid": "The proposed workflow structure no longer matches the approved repair plan.",
        "stale_workflow": "The editable workflow changed after approval. Generate and review a new preview.",
        "lock_unavailable": "The editable draft is currently being edited. Retry while this approval remains valid.",
        "validation_failed_prewrite": "The repair failed validation before the draft was changed.",
        "persistence_failed": "The editable draft could not be updated. No successful application was recorded.",
        "validation_failed_postwrite": "The persisted repair failed verification.",
        "rollback_succeeded": "Application failed, but the editable draft was restored exactly. Application provenance may require administrator review.",
        "rollback_failed": "Rollback could not restore the prior draft. Manual intervention is required before further repair attempts.",
        "already_applied": "This approved repair was already applied; no second workflow write occurred.",
        "recovery_unavailable": "This applied repair is not eligible for supervised draft restoration.",
        "recovery_invalid": "The retained recovery material does not match this applied repair.",
        "recovery_failed": "The editable draft could not be restored safely. Manual intervention is required.",
        "recovery_provenance_failed": "The editable draft was restored, but compensating provenance could not be finalized. Manual intervention is required.",
        "context_invalid": "The Fix Wizard task context is no longer actionable.",
        "preview_unavailable": "A governed structural repair preview is not currently available.",
    }
    return {
        "code": code,
        "message": messages.get(code, "The supervised structural repair could not be completed safely."),
        "retry_allowed": code == "lock_unavailable",
        "manual_intervention": code in {"rollback_failed", "recovery_provenance_failed"},
        "restored": code in {"rollback_succeeded", "recovery_provenance_failed"},
    }


@app.get("/curator/tasks/<task_id>/structural-repair-preview")
def curator_structural_repair_preview(task_id):
    from app.services.curator_structural_repair_review_service import (
        CuratorStructuralRepairReviewService,
        StructuralRepairReviewError,
    )

    fix_session_id = request.args.get("curator_session", "")
    try:
        review = CuratorStructuralRepairReviewService(
            _structural_repository_root()
        ).preview(task_id, fix_session_id)
    except StructuralRepairReviewError:
        abort(404)
    navigation = _structural_navigation(
        task_id, fix_session_id, review["item"]["item_id"],
        request.args.get("origin", ""), request.args.get("return_to", ""),
    )
    _remember_structural_preview(
        task_id, fix_session_id, review["preview"].get("preview_token", "")
    )
    return render_template(
        "curator_structural_repair_preview.html", review=review,
        task_navigation=navigation, csrf_token=_structural_csrf_token(),
    )


@app.post("/curator/tasks/<task_id>/structural-repair-approve")
def curator_structural_repair_approve(task_id):
    from app.services.curator_structural_repair_approval_service import (
        CuratorStructuralRepairApprovalService,
    )
    from app.services.curator_structural_repair_review_service import (
        CuratorStructuralRepairReviewService,
        StructuralRepairReviewError,
    )

    _require_structural_csrf()
    if request.form.get("approved") != "yes":
        abort(400)
    root = _structural_repository_root()
    fix_session_id = request.form.get("curator_session", "")
    review = None
    try:
        review = CuratorStructuralRepairReviewService(root).preview(task_id, fix_session_id)
        _require_reviewed_structural_preview(
            task_id, fix_session_id, review["preview"].get("preview_token", "")
        )
        approval = CuratorStructuralRepairApprovalService(root).issue(
            task_id=task_id,
            workflow_filename=review["workflow_filename"],
            reviewer_identity=review["fix_session"]["started_by"],
            fix_session_id=fix_session_id,
        )
        reviewed = dict(session.get("structural_repair_previews") or {})
        reviewed.pop(f"{fix_session_id}:{task_id}", None)
        session["structural_repair_previews"] = reviewed
    except (StructuralRepairReviewError, ValueError, OSError):
        failure = _structural_failure("preview_unknown")
        navigation = None
        if review:
            navigation = _structural_navigation(
                task_id, fix_session_id, review["item"]["item_id"],
                request.form.get("origin", ""), request.form.get("return_to", ""),
            )
        return render_template(
            "curator_structural_repair_result.html", result=None, failure=failure,
            approval=None, task_id=task_id, fix_session_id=fix_session_id,
            task_navigation=navigation, csrf_token=_structural_csrf_token(),
        ), 409
    return redirect(url_for(
        "curator_structural_repair_approved", approval_id=approval.approval_id,
        curator_session=fix_session_id, origin=request.form.get("origin", ""),
        return_to=request.form.get("return_to", ""),
    ))


@app.get("/curator/structural-repairs/<approval_id>")
def curator_structural_repair_approved(approval_id):
    from app.services.curator_structural_repair_review_service import (
        CuratorStructuralRepairReviewService,
        StructuralRepairReviewError,
    )

    fix_session_id = request.args.get("curator_session", "")
    try:
        review = CuratorStructuralRepairReviewService(
            _structural_repository_root()
        ).approved(approval_id, fix_session_id)
    except StructuralRepairReviewError:
        abort(404)
    navigation = _structural_navigation(
        review["task"]["task_id"], fix_session_id, review["item"]["item_id"],
        request.args.get("origin", ""), request.args.get("return_to", ""),
    )
    return render_template(
        "curator_structural_repair_approved.html", review=review,
        task_navigation=navigation, csrf_token=_structural_csrf_token(),
    )


@app.post("/curator/structural-repairs/<approval_id>/apply")
def curator_structural_repair_apply(approval_id):
    from app.services.curator_structural_repair_apply_service import (
        CuratorStructuralRepairApplyService,
        StructuralRepairApplyError,
    )
    from app.services.curator_structural_repair_review_service import (
        CuratorStructuralRepairReviewService,
        StructuralRepairReviewError,
    )

    _require_structural_csrf()
    root = _structural_repository_root()
    fix_session_id = request.form.get("curator_session", "")
    review = None
    try:
        review = CuratorStructuralRepairReviewService(root).approved(approval_id, fix_session_id)
        result = CuratorStructuralRepairApplyService(root).apply(
            approval_id,
            reviewer_identity=review["fix_session"]["started_by"],
            fix_session_id=fix_session_id,
        )
        failure = None
    except StructuralRepairReviewError as error:
        result, failure = None, _structural_failure(error.code)
    except StructuralRepairApplyError as error:
        result, failure = None, _structural_failure(error.code)
    navigation = None
    task_id = review["task"]["task_id"] if review else ""
    if review:
        navigation = _structural_navigation(
            task_id, fix_session_id, review["item"]["item_id"],
            request.form.get("origin", ""), request.form.get("return_to", ""),
        )
    status_code = 200 if result else (409 if failure and failure["retry_allowed"] else 422)
    return render_template(
        "curator_structural_repair_result.html", result=result, failure=failure,
        approval=review["approval"] if review else None, task_id=task_id,
        fix_session_id=fix_session_id, task_navigation=navigation,
        route_changes=(CuratorStructuralRepairReviewService.route_changes(review["preview"])
                       if result and review else ()),
        csrf_token=_structural_csrf_token(),
    ), status_code


@app.route("/curator/structural-repairs/<application_id>/restore", methods=["GET", "POST"])
def curator_structural_repair_restore(application_id):
    from app.services.curator_structural_repair_recovery_service import (
        CuratorStructuralRepairRecoveryService,
        StructuralRepairRecoveryError,
    )

    root = _structural_repository_root()
    fix_session_id = request.values.get("curator_session", "")
    service = CuratorStructuralRepairRecoveryService(root)
    try:
        context = service.context(application_id, fix_session_id)
    except StructuralRepairRecoveryError:
        abort(404)
    navigation = _structural_navigation(
        context["task"]["task_id"], fix_session_id, context["item"]["item_id"],
        request.values.get("origin", ""), request.values.get("return_to", ""),
    )
    result = None
    failure = None
    if request.method == "POST":
        _require_structural_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        try:
            result = service.restore(
                application_id,
                reviewer_identity=context["fix_session"]["started_by"],
                fix_session_id=fix_session_id,
                reason=request.form.get("reason", ""),
            )
        except StructuralRepairRecoveryError as error:
            failure = _structural_failure(error.code)
    return render_template(
        "curator_structural_repair_restore.html",
        context=context, result=result, failure=failure,
        fix_session_id=fix_session_id, task_navigation=navigation,
        csrf_token=_structural_csrf_token(),
    ), (200 if not failure else 422)


@app.route("/curator/tasks/<task_id>/repair-preview", methods=["GET", "POST"])
def curator_task_repair_preview(task_id):
    from app.services.curator_article_link_repair_service import (
        CuratorArticleLinkRepairError, CuratorArticleLinkRepairService,
    )
    session_id = request.values.get("curator_session", "")
    navigation = CuratorTaskNavigationService.resolve(
        request.values.get("origin", ""), request.values.get("return_to", ""), task_id=task_id
    )
    error = ""
    if request.method == "POST":
        try:
            result = CuratorArticleLinkRepairService().apply(
                task_id, session_id=session_id,
                preview_token=request.form.get("preview_token", ""),
                approved=request.form.get("approved") == "yes",
            )
            return redirect(url_for("curator_fix_session", session_id=session_id,
                                    item=result.get("next_item_id") or None,
                                    status="repair_completed"))
        except (CuratorArticleLinkRepairError, CuratorMemoryError, CuratorFixSessionError) as caught:
            error = str(caught)
    try:
        task = CuratorTaskService().get(task_id, session_id=session_id,
                                        origin=navigation.origin, return_to=navigation.return_url)
    except CuratorMemoryError:
        abort(404)
    return render_template("curator_repair_preview.html", task=task,
                           curator_session=session_id, repair_error=error,
                           task_navigation=navigation)


@app.route("/curator/run", methods=["POST"])
def run_curator_audit():
    try:
        CuratorDashboardService().run_audit()
        status = "completed"
    except AuditAlreadyRunningError:
        status = "running"
    except CuratorGovernanceError:
        status = "failed"
    except Exception as error:
        app.logger.error(json.dumps({"event": "curator_audit_failed", "request_id": g.request_id, "error_type": type(error).__name__}))
        status = "failed"
    return redirect(url_for("curator_dashboard", status=status))


@app.route("/curator/fix", methods=["GET", "POST"])
def curator_fix_start():
    integrity = KnowledgeIntegrityService().report()
    session_service = CuratorFixSessionService()
    sessions = session_service.list_sessions()
    if request.method == "POST":
        try:
            memory = CuratorMemoryStore(Path(app.root_path).parent / "curation_memory").load()
            audits = memory.get("audits", [])
            audit_id = audits[-1].get("run_id") if audits else None
            session, resumed = session_service.create_or_resume(
                started_by=request.form.get("reviewer", ""), originating_audit_id=audit_id,
                queue=CuratorRepairPlanner().build(integrity), baseline=integrity,
            )
            return redirect(url_for("curator_fix_session", session_id=session["session_id"],
                                    status="resumed" if resumed else "created"))
        except (CuratorFixSessionError, CuratorMemoryError) as error:
            app.logger.warning(json.dumps({"event": "curator_fix_session_rejected",
                                           "request_id": g.request_id,
                                           "error_type": type(error).__name__}))
            return render_template("curator_fix_start.html", integrity=integrity, sessions=sessions,
                                   error=str(error)), 400
        except Exception as error:
            app.logger.exception(json.dumps({"event": "curator_fix_session_failed",
                                              "request_id": g.request_id,
                                              "error_type": type(error).__name__}))
            return render_template(
                "curator_fix_start.html", integrity=integrity,
                sessions=session_service.list_sessions(),
                error=f"The maintenance session could not be created. Your knowledge data was not changed. "
                      f"Please retry. Request ID: {g.request_id}",
            ), 503
    return render_template("curator_fix_start.html", integrity=integrity, sessions=sessions, error=None)


@app.route("/curator/fix/<session_id>")
def curator_fix_session(session_id):
    try:
        session = CuratorFixSessionService().get(session_id)
    except CuratorFixSessionError:
        abort(404)
    item_id = request.args.get("item", "")
    category = request.args.get("category", "all")
    item = next((entry for entry in session["repair_queue"]
                 if entry["item_id"] == item_id and entry.get("status", "open") == "open"
                 and (category == "all" or entry.get("finding_type") == category)), None)
    if not item:
        item = next((entry for entry in session["repair_queue"]
                     if entry.get("status") == "open" and
                     (category == "all" or entry.get("finding_type") == category)), None)
    progress = CuratorFixSessionService.progress(
        session, category=category, current_item_id=item.get("item_id", "") if item else "")
    return render_template("curator_fix_wizard.html", session=session, item=item, progress=progress,
                           category=category, status=request.args.get("status", ""),
                           error=request.args.get("error", ""))


@app.post("/curator/fix/<session_id>/refresh")
def curator_fix_refresh(session_id):
    try:
        session = CuratorSessionReconciliationService().reconcile(session_id, trigger="manual_refresh")
    except CuratorFixSessionError:
        abort(404)
    changed = bool(session.get("last_reconciliation", {}).get("changed"))
    status = "reconciled_changed" if changed else "reconciled_unchanged"
    return redirect(url_for("curator_fix_session", session_id=session_id, status=status))


@app.post("/curator/fix/<session_id>/items/<item_id>")
def curator_fix_item_action(session_id, item_id):
    sessions = CuratorFixSessionService()
    category = request.form.get("category", "all")
    try:
        session = CuratorSessionReconciliationService().reconcile(
            session_id, trigger="targeted_execution")
        item = next((entry for entry in session["repair_queue"] if entry["item_id"] == item_id), None)
        if not item:
            raise CuratorFixSessionError("Repair item was not found.")
        action = request.form.get("action", "")
        if action == "apply":
            result = CuratorRepairExecutor().apply(item, session_id=session_id,
                                                    confirmed=request.form.get("confirmed") == "yes")
            sessions.record(session_id, item_id, "completed", note="Verified deterministic repair applied.",
                            verification=result["verification"], current=result["current_integrity"])
            status = "applied"
        elif action == "approve_legacy":
            checks = request.form.getlist("review_check")
            result = CuratorRepairExecutor().approve_legacy_validation(
                item, session_id=session_id, reviewer=session["started_by"],
                confirmed=request.form.get("confirmed") == "yes" and len(checks) == 4,
            )
            sessions.record(session_id, item_id, "completed", note="Current legacy validation approved.",
                            verification=result["verification"], current=result["current_integrity"])
            status = "legacy_validated"
        elif action in {"deferred", "skipped", "rejected"}:
            sessions.record(session_id, item_id, action, note=request.form.get("note", ""))
            status = action
        elif action == "keep_standalone" and item["classification"] == "AMBIGUOUS":
            sessions.record(session_id, item_id, "completed",
                            note="Reviewer confirmed this published article is valid standalone content.",
                            current=KnowledgeIntegrityService().report())
            status = "recorded"
        else:
            raise CuratorFixSessionError("Unsupported or unsafe maintenance action.")
    except (CuratorFixSessionError, CuratorRepairError, OSError, ValueError) as error:
        return redirect(url_for("curator_fix_session", session_id=session_id, item=item_id,
                                category=category, error=str(error)))
    return redirect(url_for("curator_fix_session", session_id=session_id,
                            category=category, status=status))


@app.route("/curator/fix/<session_id>/safe-items", methods=["GET", "POST"])
def curator_fix_safe_items(session_id):
    sessions = CuratorFixSessionService()
    try:
        session = CuratorSessionReconciliationService().reconcile(
            session_id, trigger="safe_execution" if request.method == "POST" else "safe_preview")
        safe_items = [item for item in session["repair_queue"]
                      if item.get("status") == "open" and item.get("safe_automatic")
                      and item.get("classification") in CuratorRepairExecutor.ALLOWED]
        previews = [CuratorRepairExecutor().preview(item) for item in safe_items]
        if request.method == "POST":
            if request.form.get("confirmed") != "yes":
                raise CuratorRepairError("Confirm the complete safe-repair preview before applying it.")
            for item in safe_items:
                session = CuratorSessionReconciliationService().reconcile(
                    session_id, trigger="pre_repair_verification")
                item = next((entry for entry in session["repair_queue"]
                             if entry["item_id"] == item["item_id"] and entry.get("status") == "open"), None)
                if not item or not item.get("safe_automatic") or item.get("classification") not in CuratorRepairExecutor.ALLOWED:
                    continue
                result = CuratorRepairExecutor().apply(item, session_id=session_id, confirmed=True)
                sessions.record(session_id, item["item_id"], "completed",
                                note="Verified by Repair All Safe Items.", verification=result["verification"],
                                current=result["current_integrity"])
            return redirect(url_for("curator_fix_session", session_id=session_id, status="safe_applied"))
    except (CuratorFixSessionError, CuratorRepairError, OSError, ValueError) as error:
        if request.method == "POST":
            return redirect(url_for("curator_fix_session", session_id=session_id, error=str(error)))
        abort(404)
    return render_template("curator_fix_safe_preview.html", session=session, previews=previews)


@app.route("/curator/fix/<session_id>/complete", methods=["GET", "POST"])
def curator_fix_complete(session_id):
    sessions = CuratorFixSessionService()
    try:
        session = sessions.finish(session_id, KnowledgeIntegrityService().report()) if request.method == "POST" else sessions.get(session_id)
    except CuratorFixSessionError:
        abort(404)
    return render_template(
        "curator_fix_complete.html",
        session=session,
        progress=sessions.progress(session),
    )

@app.route("/curator/integrity")
def knowledge_integrity_dashboard():
    return render_template(
        "knowledge_integrity.html",
        integrity=KnowledgeIntegrityService().report(),
        status=request.args.get("status", ""),
    )


@app.post("/curator/integrity/reindex")
def knowledge_integrity_reindex():
    KnowledgeIntegrityService().rebuild_index()
    return redirect(url_for("knowledge_integrity_dashboard", status="reindexed"))


@app.post("/curator/integrity/normalize-identities")
def knowledge_integrity_normalize_identities():
    result = KnowledgeIntegrityService().normalize_identities()
    return redirect(url_for("knowledge_integrity_dashboard", status="normalized", changed=result["count"]))


@app.route("/curator/integrity/merge", methods=["GET", "POST"])
def knowledge_integrity_merge():
    service = KnowledgeIntegrityService()
    report = service.report()
    previews = []
    for group in report["duplicate_groups"]:
        ids = [record["id"] for record in group["records"] if record["state"] == "published"]
        if len(ids) > 1:
            try:
                preview = service.merge_preview(ids[0], ids[1:])
                preview["match_reason"] = group.get("reason")
                preview["confidence"] = group.get("confidence")
                preview["identity_reasoning"] = group.get("identity_reasoning")
                previews.append(preview)
            except (KnowledgeIntegrityError, KnowledgeRepositoryError):
                pass
    if request.method == "POST":
        if request.form.get("confirmed") != "yes":
            return render_template("knowledge_merge.html", integrity=report, previews=previews, error="Confirm the merge after reviewing every record."), 400
        try:
            record_ids = request.form.getlist("record_id")
            canonical_id = request.form.get("canonical_id", "")
            service.merge(canonical_id, [item for item in record_ids if item != canonical_id])
        except (KnowledgeIntegrityError, KnowledgeRepositoryError, ArticleNotFoundError) as error:
            return render_template("knowledge_merge.html", integrity=report, previews=previews, error=str(error)), 400
        return_to = request.form.get("return_to", "")
        if return_to.startswith("/curator/fix/"):
            return redirect(return_to)
        return redirect(url_for("knowledge_integrity_dashboard", status="merged"))
    return render_template("knowledge_merge.html", integrity=report, previews=previews, error=None,
                           return_to=request.args.get("return_to", ""))


@app.route("/knowledge/manage/<article_id>")
def manage_knowledge_article(article_id):
    try:
        policy = KnowledgeIntegrityService().lifecycle_policy(article_id)
    except KnowledgeIntegrityError:
        abort(404)
    requested_return = request.args.get("return_to", "")
    return_to = (
        CuratorTaskNavigationService.valid_published_context(requested_return)
        or "/knowledge/published"
    )
    return render_template(
        "knowledge_manage.html", policy=policy, error=request.args.get("error", ""),
        return_to=return_to,
        return_label=(
            "Return to article"
            if return_to.startswith("/knowledge/published/")
            else "Return to Published Articles"
        ),
    )


@app.post("/knowledge/manage/<article_id>/<action>")
def knowledge_lifecycle_action(article_id, action):
    service = KnowledgeIntegrityService()
    try:
        if action == "archive": service.archive(article_id)
        elif action == "soft-delete": service.soft_delete(article_id)
        elif action == "permanent-delete": service.permanent_delete(article_id, request.form.get("confirmation", ""))
        else: abort(404)
    except (KnowledgeIntegrityError, KnowledgeRepositoryError) as error:
        return redirect(url_for("manage_knowledge_article", article_id=article_id, error=str(error)))
    return redirect(url_for("knowledge_integrity_dashboard", status=action))

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
            "progress_mode": (
                "branch_aware"
                if workflow.get("progress_mode") == "branch_aware"
                else "static"
            ),
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
    engine = DecisionEngine()
    engine.load_workflow(workflow_id)
    try:
        filename = draft_service.ensure_editable_copy(
            workflow_id, engine.workflow, source_type="built_in"
        )
    except WorkflowDraftError:
        return error_response(
            400,
            "This workflow cannot be copied yet",
            "The built-in workflow must pass validation before an editable copy can be created.",
        )
    return redirect(url_for("workflow_editor", filename=filename))

@app.route("/workflow-editor/<filename>")
def workflow_editor(filename):

    repository_root = _workflow_repository_root()
    draft_service = WorkflowDraftService(repository_root / "app" / "workflow_drafts")

    workflow = draft_service.get_draft(
        filename
    )

    if workflow is None:
        abort(404)

    lifecycle_projection = WorkflowLifecycleProjectionService(
        repository_root
    ).project(str(workflow.get("workflow_id") or ""))
    lifecycle_view = workflow_lifecycle_view(lifecycle_projection)

    statistics = (
        WorkflowStatisticsService()
        .build(workflow)
    )

    nodes = (
        WorkflowNodeService()
        .build(workflow)
    )

    curator_return = request.args.get("curator_return", "")
    if curator_return and not curator_return.startswith("/curator/tasks/"):
        curator_return = ""
    return_to = safe_internal_return(
        request.args.get("return_to", ""),
        ("/workflow-studio", "/content-quality"),
    )
    return render_template(
        "workflow_editor.html",
        workflow=workflow,
        statistics=statistics,
        nodes=nodes,
        filename=filename,
        workflow_category=workflow_category(workflow),
        workflow_platform=workflow_platform(workflow),
        curator_session=request.args.get("curator_session", ""),
        curator_item=request.args.get("curator_item", ""),
        curator_task=request.args.get("curator_task", ""),
        curator_return=curator_return,
        curator_category=request.args.get("category", "all"),
        lifecycle_projection=lifecycle_projection,
        lifecycle_view=lifecycle_view,
        publication_review_csrf=_publication_review_csrf_token(),
        publication_review_status=request.args.get("publication_review_status", ""),
        return_to=return_to,
        return_label=(
            "Back to Content Quality"
            if return_to.startswith("/content-quality")
            else "Back to Workflow Studio"
        ),
    )


def _workflow_repository_root() -> Path:
    configured = app.config.get("WORKFLOW_REPOSITORY_ROOT")
    return Path(configured).resolve() if configured else Path(app.root_path).parent.resolve()


def _publication_review_csrf_token() -> str:
    token = session.get("workflow_publication_review_csrf")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["workflow_publication_review_csrf"] = token
    return token


def _require_publication_review_csrf() -> None:
    supplied = str(request.form.get("csrf_token") or "")
    expected = str(session.get("workflow_publication_review_csrf") or "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400)


@app.post("/workflow-editor/<filename>/publication-reasoning-review")
def workflow_publication_reasoning_review(filename):
    _require_publication_review_csrf()
    root = _workflow_repository_root()
    workflow = WorkflowDraftService(root / "app" / "workflow_drafts").get_draft(filename)
    if workflow is None:
        abort(404)
    workflow_id = str(workflow.get("workflow_id") or "")
    projection = WorkflowLifecycleProjectionService(root).project(workflow_id)
    finding_id = str(request.form.get("finding_id") or "").strip()
    expected_fingerprint = str(request.form.get("draft_semantic_fingerprint") or "").strip()
    reviewer = str(request.form.get("reviewer") or "").strip()
    note = str(request.form.get("note") or "").strip()
    finding = next(
        (item for item in projection.reasoning_reviews if item.finding_id == finding_id), None
    )
    status = "invalid"
    try:
        if (projection.reasoning_review_error or not finding
                or finding.review_status == "accepted"
                or expected_fingerprint != projection.draft_semantic_fingerprint):
            raise WorkflowPublicationReviewRepositoryError(
                "The publication review no longer matches the current draft."
            )
        review = WorkflowPublicationReasoningReview.create(
            workflow_id=workflow_id,
            draft_semantic_fingerprint=projection.draft_semantic_fingerprint,
            finding_id=finding.finding_id,
            rule=finding.rule,
            finding_type=finding.finding_type,
            content_identifier=finding.content_identifier,
            node_id=finding.node_id,
            reviewer=reviewer,
            reviewed_at=datetime.now(timezone.utc).isoformat(),
            note=note,
        )
        WorkflowPublicationReviewRepository(root / "curation_memory").add(review)
        status = "accepted"
    except WorkflowPublicationReviewRepositoryError:
        status = "invalid"
    return redirect(url_for(
        "workflow_editor", filename=filename,
        publication_review_status=status,
        _anchor="workflowReasoningPublicationReview",
    ))


def workflow_lifecycle_view(projection):
    lifecycle_labels = {
        "MATCHES_PUBLISHED": "Matches published",
        "GOVERNED_CHANGES": "Governed unpublished changes",
        "AUTHORED_OR_UNATTRIBUTED_CHANGES": "Authored/unattributed changes",
        "MIXED_CHANGES": "Mixed unpublished changes",
        "AMBIGUOUS_STATE": "Ambiguous lifecycle state",
        "NO_ACTIVE_PUBLICATION": "No active publication",
    }
    publication_labels = {
        "READY_FOR_PUBLICATION_REVIEW": "READY FOR PUBLICATION REVIEW",
        "NOT_READY": "NOT READY FOR PUBLICATION REVIEW",
        "NO_UNPUBLISHED_CHANGES": "NO UNPUBLISHED CHANGES",
    }
    version = projection.active_published_version
    header = lifecycle_labels.get(projection.lifecycle_state, projection.lifecycle_state)
    if version is not None and projection.lifecycle_state != "AMBIGUOUS_STATE":
        header += f" · Published v{version}"
    governed_count = sum(item.provenance == "governed" for item in projection.semantic_delta)
    authored_count = len(projection.semantic_delta) - governed_count
    changes = []
    for item in projection.semantic_delta:
        if item.path.startswith("/nodes/") and "/knowledge_article" in item.path:
            category = "Knowledge relationship"
        elif item.path.startswith("/nodes/") and any(
                marker in item.path for marker in ("/answers/", "/next", "/skip_to")):
            category = "Route/transition"
        elif item.path.startswith("/nodes/"):
            category = "Node"
        elif item.path.count("/") == 1:
            category = "Workflow metadata"
        else:
            category = "Other workflow field"
        changes.append({
            "category": category,
            "operation": item.operation,
            "path": item.path,
            "before": item.before_summary,
            "after": item.after_summary,
            "provenance": item.provenance,
        })
    return {
        "header_label": header,
        "lifecycle_state": projection.lifecycle_state,
        "lifecycle_label": lifecycle_labels.get(
            projection.lifecycle_state, projection.lifecycle_state
        ),
        "review_label": publication_labels.get(
            projection.publication_review_state, projection.publication_review_state
        ),
        "review_state": projection.publication_review_state,
        "active_version": version,
        "runtime_version": projection.runtime.selected_version,
        "runtime_aligned": projection.runtime.matches_active_publication,
        "runtime_overlay_present": projection.runtime.runtime_overlay_present,
        "change_count": len(projection.semantic_delta),
        "governed_count": governed_count,
        "authored_count": authored_count,
        "changes": changes,
        "readiness_reasons": list(projection.readiness_reasons),
        "ambiguity_reasons": list(projection.ambiguity_reasons),
        "has_unattributed_changes": authored_count > 0,
        "validation_clean": bool(
            projection.validation.schema_valid
            and projection.validation.graph_valid
            and not projection.validation.errors
            and not projection.validation.warnings
            and projection.validation.quality_status != "ERROR"
        ),
        "draft_semantic_fingerprint": projection.draft_semantic_fingerprint,
        "reasoning_review_error": projection.reasoning_review_error,
        "reasoning_reviews": [asdict(item) for item in projection.reasoning_reviews],
    }


@app.route("/api/workflow-drafts/<filename>/lifecycle")
def workflow_lifecycle_status(filename):
    root = _workflow_repository_root()
    workflow = WorkflowDraftService(root / "app" / "workflow_drafts").get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404
    projection = WorkflowLifecycleProjectionService(
        root
    ).project(str(workflow.get("workflow_id") or ""))
    return {
        "ok": True,
        "projection": asdict(projection),
        "view": workflow_lifecycle_view(projection),
    }


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
    curator_session = request.args.get("curator_session", "")
    curator_item = request.args.get("curator_item", "")
    return_to = ""
    if curator_session:
        return_to = url_for(
            "curator_fix_session",
            session_id=curator_session,
            item=curator_item,
        )
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
        existing = ArticleIdentityResolver(knowledge_repository).resolve(candidate=article, include_drafts=True)
        if existing:
            workflow = draft_service.update_node(filename, node_id, {"knowledge_article": existing.article["id"]})
            normalized = next(item for item in WorkflowNodeService().build(workflow) if item["id"] == node_id)
            return {"ok": True, "created": False, "reused": True, "article_id": existing.article["id"],
                    "duplicate_confidence": round(existing.confidence * 100, 1),
                    "review_url": (url_for("view_published", article_id=existing.article["id"], return_to=return_to) if return_to else url_for("view_published", article_id=existing.article["id"])) if any(a.get("id") == existing.article["id"] for a in knowledge_repository.get_published()) else (url_for("review_draft", article_id=existing.article["id"], return_to=return_to) if return_to else url_for("review_draft", article_id=existing.article["id"])),
                    "node": normalized}, 200
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
        "review_url": url_for("review_draft", article_id=article["id"], return_to=return_to) if return_to else url_for("review_draft", article_id=article["id"]),
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
    root = _workflow_repository_root()
    workflow = WorkflowDraftService(root / "app" / "workflow_drafts").get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    try:
        publication_service = WorkflowPublicationService(
            root / "app" / "workflow_publications"
        )
        status = publication_service.status(workflow.get("workflow_id"))
    except WorkflowPublicationError as error:
        return {"ok": False, "error": str(error)}, 400

    latest_hash = status["versions"][0]["content_hash"] if status["versions"] else None
    lifecycle = WorkflowLifecycleProjectionService(root).project(
        str(workflow.get("workflow_id") or "")
    )
    return {
        "ok": True,
        **status,
        "has_unpublished_changes": latest_hash != publication_service.content_hash(workflow),
        "publication_reasoning_review_ready": (
            not lifecycle.reasoning_review_error
            and all(item.review_status == "accepted" for item in lifecycle.reasoning_reviews)
        ),
    }


@app.route(
    "/api/workflow-drafts/<filename>/publication",
    methods=["POST"],
)
def publish_workflow_draft(filename):
    root = _workflow_repository_root()
    workflow = WorkflowDraftService(root / "app" / "workflow_drafts").get_draft(filename)
    if workflow is None:
        return {"ok": False, "error": "Workflow draft not found."}, 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "A JSON object is required."}, 400

    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        return {"ok": False, "error": "Version label must be text."}, 400

    lifecycle = WorkflowLifecycleProjectionService(root).project(
        str(workflow.get("workflow_id") or "")
    )
    if (lifecycle.reasoning_review_error
            or any(item.review_status != "accepted" for item in lifecycle.reasoning_reviews)):
        return {
            "ok": False,
            "error": "Current deterministic reasoning findings require explicit publication acceptance.",
        }, 409

    try:
        publication_service = WorkflowPublicationService(
            root / "app" / "workflow_publications"
        )
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

    query = request.args.get("q", "").strip()
    selected_category = request.args.get("category", "").strip()
    all_commands = command_repository.get_all()
    category_counts = {}
    for command in all_commands:
        category = command.get("category") or "Uncategorized"
        category_counts[category] = category_counts.get(category, 0) + 1

    commands = []
    normalized_query = query.casefold()
    normalized_category = selected_category.casefold()
    for command in all_commands:
        category = command.get("category") or "Uncategorized"
        if normalized_category and category.casefold() != normalized_category:
            continue
        tags = command.get("tags", [])
        platforms = command.get("platforms", [])
        searchable_text = " ".join([
            str(command.get("name", "")),
            str(command.get("title", "")),
            str(command.get("summary", "")),
            str(category),
            str(command.get("shell", "")),
            " ".join(str(tag) for tag in tags if tags),
            " ".join(str(platform) for platform in platforms if platforms),
        ]).casefold()
        if normalized_query and normalized_query not in searchable_text:
            continue
        commands.append(command)

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
        query=query,
        selected_category=selected_category,
        categories=sorted(category_counts.items(), key=lambda item: item[0].casefold()),
        result_count=len(commands),
        total_count=len(all_commands),
        inventory_empty=not all_commands,
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

    requested_return = request.args.get("return_to", "")
    task_return = CuratorTaskNavigationService.valid_task_return(requested_return)
    return_to = task_return or safe_internal_return(
        requested_return, ("/commands", "/search")
    )
    return render_template(
        "command.html",
        command=command,
        related_articles=related_articles,
        related_commands=related_commands,
        explanation=explanation,
        return_to=return_to,
        return_label=(
            "Return to Curator task" if task_return
            else "Back to Search Results" if return_to.startswith("/search")
            else "Back to Command Library"
        ),
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
    results = (
        search_service.search_all(query, context=device_context)
        if query
        else []
    )
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
    return_to = request.form.get("return_to", "") if request.method == "POST" else request.args.get("return_to", "")
    return_to = (
        CuratorTaskNavigationService.valid_maintenance_return(return_to)
        or CuratorTaskNavigationService.valid_assisted_return(return_to)
    )
    return render_template(
        "draft_review.html",
        article=article,
        analysis=ArticleReviewService().analyze(article),
        workflow_references=workflow_references_for_article(article.get("id")),
        workflow_return_url=return_to or workflow_return_location(article),
        return_to=return_to,
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
                return_location = request.form.get("return_to", "")
                return_location = (
                    CuratorTaskNavigationService.valid_maintenance_return(return_location)
                    or CuratorTaskNavigationService.valid_assisted_return(return_location)
                )
                if not return_location:
                    return_location = workflow_return_location(article)
                KnowledgePublicationService(knowledge_repository, WorkflowDraftService()).publish(
                    article_id,
                    reviewer=article.get("review", {}).get("reviewed_by") or "Gnojo reviewer",
                )
                return redirect(return_location or url_for("view_published", article_id=article_id))
        except (ArticleReviewError, KnowledgeRepositoryError, KnowledgePublicationError) as error:
            return render_article_review(article, str(error), 400)
        return_to = request.form.get("return_to", "")
        return redirect(url_for("review_draft", article_id=article_id, saved="1", return_to=return_to))

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

    query = request.args.get("q", "").strip()
    selected_category = request.args.get("category", "").strip()

    all_articles = knowledge_repository.get_published()
    for article in all_articles:
        if not article.get("tags"):
            article["tags"] = ArticleTagService.generate(article)

    category_counts = {}
    for article in all_articles:
        category = article.get("category") or "Uncategorized"
        category_counts[category] = category_counts.get(category, 0) + 1

    articles = []
    normalized_query = query.casefold()
    normalized_category = selected_category.casefold()
    for article in all_articles:
        category = article.get("category") or "Uncategorized"
        if normalized_category and category.casefold() != normalized_category:
            continue

        tags = article.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        searchable_text = " ".join([
            str(article.get("title", "")),
            str(article.get("overview", "")),
            str(category),
            str(article.get("difficulty", "")),
            " ".join(str(tag) for tag in tags),
        ]).casefold()

        if normalized_query and normalized_query not in searchable_text:
            continue
        articles.append(article)

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
        selected_category=selected_category,
        categories=sorted(category_counts.items(), key=lambda item: item[0].casefold()),
        result_count=len(articles),
        total_count=len(all_articles),
        inventory_empty=not all_articles,
    )


@app.route("/knowledge/published/<article_id>")
def view_published(article_id):
    """
    Display one published knowledge article.
    """

    try:
        article = knowledge_repository.resolve_published_article(
            article_id
        )

        related_articles = []

        for related_id in article.get(
            "related_articles",
            [],
        ):
            try:
                related_article = (
                    knowledge_repository.resolve_published_article(
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
                article["id"]
            )
        )

    except ArticleNotFoundError:
        abort(404)

    except KnowledgeRepositoryError:
        abort(500)

    template_name = "published_article.html"

    if article.get("type") == "command":
        template_name = "published_command.html"

    requested_return = request.args.get("return_to", "")
    task_return = CuratorTaskNavigationService.valid_task_return(requested_return)
    return_to = task_return or (
        CuratorTaskNavigationService.valid_assisted_return(requested_return)
        or CuratorTaskNavigationService.valid_maintenance_return(requested_return)
    )
    if not return_to:
        return_to = safe_internal_return(
            requested_return, ("/knowledge/published", "/search"),
        )
    return render_template(
        template_name,
        article=article,
        related_articles=related_articles,
        related_commands=related_commands,
        return_to=return_to,
        manage_return_context=url_for(
            "view_published", article_id=article["id"],
            return_to=(
                return_to
                if (
                    CuratorTaskNavigationService.valid_published_context(return_to)
                    or CuratorTaskNavigationService.valid_task_return(return_to)
                )
                else None
            ),
        ),
        return_label=(
            "Return to Curator task" if task_return
            else "Back to Assisted Resolution" if return_to.startswith("/curator/tasks/")
            else "Back to Fix Wizard" if return_to.startswith("/curator/fix")
            else "Back to Search Results" if return_to.startswith("/search")
            else "Back to Published Articles"
        ),
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

        KnowledgePublicationService(knowledge_repository, WorkflowDraftService()).publish(
            article_id,
            reviewer=article.get("review", {}).get("reviewed_by") or "Gnojo reviewer",
        )

    except ArticleNotFoundError:
        abort(404)

    except (KnowledgeRepositoryError, KnowledgePublicationError):
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
                int(previous_location.get("step", session.get("step", 1) - 1)),
                1,
            )
            continuation = session.get("workflow_continuation")
            if continuation and previous_workflow == continuation.get("origin_workflow"):
                session.pop("workflow_continuation", None)
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

        is_workflow_handoff = current_node.type == "transition" or (
            current_node.type == "resolution" and current_node.next_workflow
        )
        if is_workflow_handoff:
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
                    "step": session.get("step", 1),
                }
            )

            session["node_history"] = node_history
            session["workflow"] = next_workflow
            session["current_node"] = next_node.id
            session["workflow_version"] = next_version
            session["step"] = 0
            if current_node.type == "resolution":
                session["workflow_continuation"] = {
                    "origin_workflow": workflow_name,
                    "origin_name": workflow_catalog[workflow_name]["name"],
                    "destination_workflow": next_workflow,
                }
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
                    "step": session.get("step", 1),
                }
            )
        
            session["node_history"] = node_history
            session["current_node"] = next_node.id

            estimated_steps = engine.workflow.get(
                "estimated_steps",
                5,
            )

            current_step = session.get("step", 1)

            session["step"] = (
                current_step + 1
                if WorkflowProgressService.enabled(engine.workflow)
                else min(current_step + 1, estimated_steps)
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
    session.pop("workflow_continuation", None)
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
            session_environment=troubleshooting_session_environment(),
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


def troubleshooting_session_environment():
    """Resolve one authoritative environment label for a newly created session."""
    configured = str(os.getenv("GNOJO_SESSION_ENVIRONMENT") or "").strip().lower()
    if configured:
        if configured in TROUBLESHOOTING_SESSION_ENVIRONMENTS:
            return configured
        app.logger.warning(
            "Invalid GNOJO_SESSION_ENVIRONMENT; defaulting troubleshooting history to production."
        )
        return "production"
    if app.config.get("TESTING"):
        return "test"
    if app.debug:
        return "development"
    return "production"


def render_wizard(engine, node, knowledge, workflow_catalog=None):
    """
    Render the shared wizard template with workflow progress
    and optional knowledge article content.
    """

    workflow_name = session["workflow"]
    workflow_info = (workflow_catalog or available_workflows())[workflow_name]

    branch_aware_progress = WorkflowProgressService.enabled(engine.workflow)
    estimated_steps = engine.workflow.get("estimated_steps", 5)
    current_step = session.get("step", 1)
    current_step = max(current_step, 1)
    if branch_aware_progress:
        estimated_steps = WorkflowProgressService.total(
            engine.workflow, node.id, current_step
        )

    history_record = None
    is_continuation_result = node.type == "resolution" and bool(node.next_workflow)
    if node.type == "resolution" and not is_continuation_result:
        session["workflow_complete"] = True
        if branch_aware_progress:
            estimated_steps = current_step
        else:
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
    branch_aware_progress=branch_aware_progress,
    can_go_back=bool(
        session.get("node_history")
    ),
    history_record=history_record,
    continuation_context=(
        session.get("workflow_continuation")
        if session.get("workflow_continuation", {}).get("destination_workflow") == workflow_name
        else None
    ),
)


if __name__ == "__main__":
    app.run(debug=os.getenv("GNOJO_DEBUG", "false").lower() in {"1", "true", "yes"})
