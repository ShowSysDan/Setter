"""
External integration API (/api/v1/) for Q-SYS, Crestron, AMX, Companion or
anything that speaks HTTP rather than UDP. Accepts the same control strings
as the UDP listener.

    GET  /api/v1/cmd?c=FADE+2+25
    POST /api/v1/cmd            body: FADE 2 25   (text/plain)
                                   or {"cmd": "FADE 2 25"} (JSON)
    GET  /api/v1/status

If API_KEY is set, pass it as X-API-Key or ?api_key=.
Add ?format=text to get plain "OK ..." / "ERR ..." lines instead of JSON.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, Response, current_app, jsonify, request

bp = Blueprint("external_api", __name__, url_prefix="/api/v1")


def _controller():
    return current_app.extensions["setter"]


def require_key(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        key = current_app.config.get("API_KEY")
        if key:
            supplied = request.headers.get("X-API-Key") or request.args.get("api_key")
            if supplied != key:
                return jsonify({"ok": False, "error": "invalid or missing API key"}), 401
        return view(*args, **kwargs)
    return wrapper


def _wants_text() -> bool:
    if request.args.get("format") == "text":
        return True
    accept = request.headers.get("Accept", "")
    return "text/plain" in accept and "application/json" not in accept


@bp.route("/cmd", methods=["GET", "POST"])
@require_key
def cmd():
    text = request.args.get("c") or request.args.get("cmd")
    if request.method == "POST" and not text:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            text = data.get("cmd") or data.get("text")
        elif isinstance(data, str):
            text = data
        else:
            text = request.get_data(as_text=True)
    text = (text or "").strip()
    if not text:
        return jsonify({"ok": False, "replies": ["ERR empty command"]}), 400
    replies = _controller().run(text, via="v1", sender=request.remote_addr)
    ok = all(r.startswith(("OK", "PONG")) for r in replies)
    if _wants_text():
        return Response("\n".join(replies) + "\n", mimetype="text/plain", status=200 if ok else 422)
    return jsonify({"ok": ok, "replies": replies}), 200 if ok else 422


@bp.get("/status")
@require_key
def status():
    ctl = _controller()
    if _wants_text():
        return Response(ctl.status_line() + "\n", mimetype="text/plain")
    full = ctl.status()
    sw = full["switcher"] or {}
    return jsonify({
        "connection": full["connection"],
        "program": sw.get("program"), "preview": sw.get("preview"), "aux": sw.get("aux"),
        "transition": sw.get("transition"), "mix_rate": sw.get("mix_rate"), "ftb": sw.get("ftb"),
        "dsk": sw.get("dsk"), "video_mode": sw.get("video_mode"),
        "inputs": full["inputs"],
    })
