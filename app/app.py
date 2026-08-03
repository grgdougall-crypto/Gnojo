import re

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dataclasses import asdict

from markupsafe import Markup, escape

from app.engine.decision_engine import DecisionEngine
from app.knowledge.knowledge_base import KnowledgeBase
from app.repositories.knowledge_repository import (
    ArticleNotFoundError,
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from app.repositories.command_repository import CommandRepository

from app.services.search_service import SearchService

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

knowledge_repository = KnowledgeRepository()
command_repository = CommandRepository()
search_service = SearchService()
relationship_service = RelationshipService()
explanation_service = ExplanationService()
draft_generation_service = DraftGenerationService()
publish_validation_service = PublishValidationService()
publication_service = PublicationService()


# Development only
app.secret_key = "supportpilot-development-key"

current_draft = None

AVAILABLE_WORKFLOWS = {
    "internet": {
        "name": "Internet Connection",
        "description": "Troubleshoot Wi-Fi, Ethernet, routers, and connectivity.",
        "icon": "bi-wifi",
    },
    "printer": {
        "name": "Printer",
        "description": (
            "Troubleshoot power, connections, print queues, and paper issues."
        ),
        "icon": "bi-printer",
    },
    "network_diagnostics": {
        "name": "Advanced Network Diagnostics",
        "description": "Perform advanced network diagnostics to identify and resolve connectivity issues.",
        "icon": "bi-wifi",
    }
}


@app.route("/")
def home():
    session.clear()

    return render_template(
        "index.html",
        workflows=AVAILABLE_WORKFLOWS,
    )

@app.route("/content-studio")
def content_studio():

    return render_template(
        "content_studio.html",
    )

@app.route("/workflow-studio")
def workflow_studio():

    draft_service = WorkflowDraftService()

    return render_template(
        "workflow_studio.html",
        drafts=draft_service.list_drafts(),
    )

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
    )

@app.route("/knowledge")
def knowledge_center():
    """
    Display the SupportPilot Knowledge Center.
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
    Create a new SupportPilot command draft.
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
            article.get("id")
            for article in results["articles"]
        ],
        "commands": [
            command.get("id")
            for command in results["commands"]
        ],
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

    results = search_service.search_all(query)

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

    return render_template(
        "search_results.html",
        query=query,
        results=results,
        selected_type=selected_type,
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

    results = search_service.search_all(query)

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

@app.route("/knowledge/drafts/<article_id>")
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

    return render_template(
        "draft_review.html",
        article=article,
    )

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

        article["review"]["status"] = "published"

        knowledge_repository.save_draft(
            article,
            overwrite=True,
        )

        knowledge_repository.publish_article(
            article_id,
        )

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

    if request.method == "POST":

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

        except Exception as ex:

            error = str(ex)

    return render_template(
        "workflow_builder.html",
        generated_workflow=generated_workflow,
        validation=validation,
        outline=outline,
        filename=filename,
        error=error,
    )

@app.route("/wizard", methods=["GET", "POST"])
def wizard():
    engine = DecisionEngine()
    knowledge = KnowledgeBase()

    # --------------------------------------------------
    # Process an answer or continue an instruction
    # --------------------------------------------------
    if request.method == "POST":
        workflow_name = session.get("workflow")
        current_node_id = session.get("current_node")

        if (
            workflow_name not in AVAILABLE_WORKFLOWS
            or current_node_id is None
        ):
            return redirect(url_for("home"))

        try:
            engine.load_workflow(workflow_name)
        except FileNotFoundError:
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

            if previous_workflow not in AVAILABLE_WORKFLOWS:
                abort(404)

            try:
                engine.load_workflow(previous_workflow)
            except FileNotFoundError:
                abort(404)

            previous_node = engine.get_node(previous_node_id)

            if previous_node is None:
                abort(404)

            session["node_history"] = node_history
            session["workflow"] = previous_workflow
            session["current_node"] = previous_node_id
            session["step"] = max(
                session.get("step", 1) - 1,
                1,
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

            if next_workflow not in AVAILABLE_WORKFLOWS:
                abort(404)

            try:
                engine.load_workflow(next_workflow)
            except FileNotFoundError:
                abort(404)

            next_node = engine.get_start_node()

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
                }
            )

            session["node_history"] = node_history
            session["workflow"] = next_workflow
            session["current_node"] = next_node.id
            session["step"] = 0

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
            node_history = session.get(
                "node_history",
                [],
            )

            node_history.append(
                {
                    "workflow": workflow_name,
                    "node_id": current_node.id,
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
            or workflow_name not in AVAILABLE_WORKFLOWS
            or current_node_id is None
        ):
            return redirect(url_for("home"))

        try:
            engine.load_workflow(workflow_name)
        except FileNotFoundError:
            abort(404)

        node = engine.get_node(current_node_id)

        if node is None:
            return redirect(url_for("home"))

        return render_wizard(
            engine,
            node,
            knowledge,
        )

    # --------------------------------------------------
    # Start or restart a workflow
    # --------------------------------------------------
    if workflow_name not in AVAILABLE_WORKFLOWS:
        return redirect(url_for("home"))

    try:
        engine.load_workflow(workflow_name)
    except FileNotFoundError:
        abort(404)

    node = engine.get_start_node()

    if node is None:
        abort(500)

    session["workflow"] = workflow_name
    session["current_node"] = node.id
    session["step"] = 1
    session["node_history"] = []

    return render_wizard(
        engine,
        node,
        knowledge,
    )


def render_wizard(engine, node, knowledge):
    """
    Render the shared wizard template with workflow progress
    and optional knowledge article content.
    """

    workflow_name = session["workflow"]
    workflow_info = AVAILABLE_WORKFLOWS[workflow_name]

    estimated_steps = engine.workflow.get("estimated_steps", 5)
    current_step = session.get("step", 1)
    current_step = max(current_step, 1)

    if node.type == "resolution":
        current_step = estimated_steps
        progress_percent = 100
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

    return render_template(
    "wizard.html",
    node=node,
    article=article,
    workflow_id=workflow_name,
    workflow_name=workflow_info["name"],
    current_step=current_step,
    estimated_steps=estimated_steps,
    progress_percent=progress_percent,
    can_go_back=bool(
        session.get("node_history")
    ),
)


if __name__ == "__main__":
    app.run(debug=True)