"""HTML page routes."""
from flask import Blueprint, current_app, render_template, request

bp = Blueprint("main", __name__)


def _controller():
    return current_app.extensions["setter"]


@bp.route("/")
def index():
    return render_template("index.html", status=_controller().status())


@bp.route("/settings")
def settings():
    return render_template("settings.html", status=_controller().status())


@bp.route("/reference")
def reference():
    """In-app API library: the control strings with runnable examples, and
    how to send them from each kind of control system."""
    return render_template(
        "reference.html",
        status=_controller().status(),
        setter_ip=request.host.rsplit(":", 1)[0] if ":" in request.host and not request.host.startswith("[") else request.host,
        base_url=request.host_url,
        api_key_set=bool(current_app.config.get("API_KEY")),
    )
