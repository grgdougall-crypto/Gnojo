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

app = Flask(__name__)

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


@app.route("/wizard", methods=["GET", "POST"])
def wizard():
    engine = DecisionEngine()

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

        return render_wizard(engine, node)

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

    return render_wizard(engine, node)


def render_wizard(engine, node):
    """
    Render the shared wizard template with workflow progress.
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

    return render_template(
        "wizard.html",
        node=node,
        workflow_id=workflow_name,
        workflow_name=workflow_info["name"],
        current_step=current_step,
        estimated_steps=estimated_steps,
        progress_percent=progress_percent,
    )


if __name__ == "__main__":
    app.run(debug=True)