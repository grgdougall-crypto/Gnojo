from flask import Flask, render_template, session

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


if __name__ == "__main__":
    app.run(debug=True)