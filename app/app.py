from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

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

app = Flask(__name__)

knowledge_repository = KnowledgeRepository()
command_repository = CommandRepository()
search_service = SearchService()
relationship_service = RelationshipService()

# Development only
app.secret_key = "supportpilot-development-key"

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

    return render_template(
        "command.html",
        command=command,
        related_articles=related_articles,
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