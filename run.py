import os

from app.app import app


if __name__ == "__main__":
    app.run(debug=os.getenv("GNOJO_DEBUG", "false").lower() in {"1", "true", "yes"})
