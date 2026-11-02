"""HTML page routes."""
from flask import Blueprint, current_app, render_template

bp = Blueprint("main", __name__)


def _controller():
    return current_app.extensions["setter"]


@bp.route("/")
def index():
    return render_template("index.html", status=_controller().status())


@bp.route("/settings")
def settings():
    return render_template("settings.html", status=_controller().status())
