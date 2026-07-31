import json
from pathlib import Path

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

app = Flask(__name__)

DRAFT_DIRECTORY = (
    Path(__file__).parent.parent
    / "knowledge_base"
    / "drafts"
)

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

    draft_count = 0
    published_count = 0

    if DRAFT_DIRECTORY.exists():
        draft_count = len(
            list(DRAFT_DIRECTORY.glob("*.json"))
        )

    published_directory = (
        Path(__file__).parent.parent
        / "knowledge_base"
        / "published"
    )

    if published_directory.exists():
        published_count = len(
            list(published_directory.glob("*.json"))
        )

    return render_template(
        "knowledge_center.html",
        draft_count=draft_count,
        published_count=published_count,
    )

@app.route("/knowledge/drafts")
def list_drafts():
    """
    Display all knowledge articles awaiting human review.
    """

    drafts = []

    if DRAFT_DIRECTORY.exists():

        for article_path in sorted(
            DRAFT_DIRECTORY.glob("*.json")
        ):
            try:
                with article_path.open(
                    "r",
                    encoding="utf-8",
                ) as article_file:
                    article = json.load(article_file)

            except (
                OSError,
                json.JSONDecodeError,
            ):
                continue

            if not isinstance(article, dict):
                continue

            drafts.append(article)

    return render_template(
        "drafts.html",
        drafts=drafts,
    )

@app.route("/knowledge/drafts/<article_id>")
def review_draft(article_id):
    """
    Display one draft knowledge article for human review.
    """

    article_path = DRAFT_DIRECTORY / f"{article_id}.json"

    if not article_path.exists():
        abort(404)

    try:
        with article_path.open(
            "r",
            encoding="utf-8",
        ) as article_file:
            article = json.load(article_file)

    except (
        OSError,
        json.JSONDecodeError,
    ):
        abort(500)

    if not isinstance(article, dict):
        abort(500)

    return render_template(
        "draft_review.html",
        article=article,
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