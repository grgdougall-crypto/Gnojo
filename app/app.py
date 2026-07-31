from flask import Flask, render_template, session, request
from app.engine.decision_engine import DecisionEngine

app = Flask(__name__)

# Required for Flask session storage.
# This is acceptable for local development but should be replaced
# with a secure environment variable before deployment.
app.secret_key = "supportpilot-development-key"


@app.route("/")
def home():
    session.clear()
    return render_template("index.html")


@app.route("/problems")
def problems():
    return render_template("problems.html")


@app.route("/device/internet")
def internet_device():
    session["problem"] = "Internet connection"
    return render_template("device.html")


@app.route("/connection/internet/windows")
def internet_connection():
    session["device"] = "Windows PC"
    return render_template("connection.html")


@app.route("/scope/internet/<connection_type>")
def internet_scope(connection_type):
    connection_names = {
        "wifi": "Wi-Fi",
        "ethernet": "Ethernet",
        "unknown": "Not sure",
    }

    session["connection"] = connection_names.get(
        connection_type,
        "Not sure"
    )

    return render_template("scope.html")


@app.route("/diagnosis/internet/<scope_type>")
def internet_diagnosis(scope_type):
    scope_names = {
        "single-device": "Other devices can connect",
        "all-devices": "No other devices can connect",
        "unknown": "Not sure",
    }

    session["scope"] = scope_names.get(
        scope_type,
        "Not sure"
    )

    return render_template(
        "diagnosis.html",
        problem=session.get("problem", "Not provided"),
        device=session.get("device", "Not provided"),
        connection=session.get("connection", "Not provided"),
        scope=session.get("scope", "Not provided"),
    )

@app.route("/wizard", methods=["GET", "POST"])
def wizard():

    engine = DecisionEngine()

    # First visit
    if request.method == "GET":

        engine.load_workflow("internet")

        node = engine.get_start_node()

        session["workflow"] = "internet"
        session["current_node"] = node.id

        return render_template(
            "wizard.html",
            node=node
        )

    # User submitted an answer
    engine.load_workflow(session["workflow"])

    current_node = engine.get_node(
        session["current_node"]
    )

    answer = request.form.get("answer")

    node = engine.advance(
        current_node,
        answer
    )

    if node is None:
        return "Workflow complete."

    session["current_node"] = node.id

    return render_template(
        "wizard.html",
        node=node
    )

if __name__ == "__main__":
    app.run(debug=True)