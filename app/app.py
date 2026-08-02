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
}


@app.route("/")
def home():
    session.clear()

    return render_template(
        "index.html",
        workflows=AVAILABLE_WORKFLOWS,
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
        return redirect(
            url_for("edit_command_draft")
        )

    return render_template(
        "command_builder_edit.html",
        draft=current_draft,
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

    return render_template(
        "published_article.html",
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

@app.route("/wizard", methods=["GET", "POST"])
def wizard():
    engine = DecisionEngine()
    knowledge = KnowledgeBase()

    # --------------------------------------------------
    # Start or restart a workflow
    # --------------------------------------------------
    if request.method == "GET":
        workflow_name = request.args.get("workflow")

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

        return render_wizard(
            engine,
            node,
            knowledge,
        )

    # --------------------------------------------------
    # Continue an existing workflow
    # --------------------------------------------------
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

    answer = request.form.get("answer")
    node = engine.advance(current_node, answer)

    if node is None:
        node = current_node
    else:
        session["current_node"] = node.id

        estimated_steps = engine.workflow.get("estimated_steps", 5)
        current_step = session.get("step", 1)

        session["step"] = min(
            current_step + 1,
            estimated_steps,
        )

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
    )


if __name__ == "__main__":
    app.run(debug=True)