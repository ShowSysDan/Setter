"""Internal REST API used by the dashboard (/api/)."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..__version__ import __version__
from ..services.atem_client import NotConnected
from ..services.settings import SettingsError

bp = Blueprint("api", __name__, url_prefix="/api")


def _controller():
    return current_app.extensions["setter"]


@bp.get("/version")
def version():
    return jsonify({"version": __version__})


@bp.get("/status")
def status():
    return jsonify(_controller().status())


@bp.get("/events")
def events():
    since = request.args.get("since", 0, type=int)
    return jsonify({"events": _controller().audit.since(since)})


@bp.get("/settings")
def get_settings():
    return jsonify(_controller().settings.all())


@bp.put("/settings")
@bp.post("/settings")
def put_settings():
    ctl = _controller()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "send a JSON object of settings"}), 400
    try:
        changed = ctl.settings.update(data)
    except SettingsError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    ctl.apply_settings(changed)
    return jsonify({"ok": True, "changed": changed, "settings": ctl.settings.all()})


@bp.get("/syslog")
def syslog_status():
    return jsonify(_controller().audit.remote_status())


@bp.post("/syslog/test")
def syslog_test():
    """Write a test entry; the counters in the response tell you whether it left the building."""
    import time
    ctl = _controller()
    before = ctl.audit.remote_status()
    if not before["enabled"] or not before["running"]:
        return jsonify({"ok": False, "error": "remote syslog is not enabled", "status": before}), 409
    ctl.audit.info("Test message from the Setter settings page", via="ui", sender=request.remote_addr)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        after = ctl.audit.remote_status()
        if after["sent"] > before["sent"] or after["failed"] > before["failed"]:
            break
        time.sleep(0.05)
    after = ctl.audit.remote_status()
    ok = after["sent"] > before["sent"]
    return jsonify({"ok": ok, "status": after,
                    "error": None if ok else (after["last_error"] or "no delivery confirmation within 2 s")})


@bp.post("/connect")
def connect():
    _controller().connect()
    return jsonify({"ok": True, "state": _controller().connection_state()})


@bp.post("/disconnect")
def disconnect():
    _controller().disconnect()
    return jsonify({"ok": True, "state": _controller().connection_state()})


@bp.post("/command")
def command():
    """Run a control string - exactly what the UDP listener would do with it."""
    data = request.get_json(silent=True)
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return jsonify({"ok": False, "replies": ["ERR empty command"]}), 400
    replies = _controller().run(text, via="ui", sender=request.remote_addr)
    ok = all(r.startswith(("OK", "PONG")) for r in replies)
    return jsonify({"ok": ok, "replies": replies})


@bp.post("/raw")
def raw():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "send a JSON body"}), 400
    name = str(data.get("name") or "").strip()
    hexstr = "".join(str(data.get("hex") or "").split()).replace(":", "")
    if len(name) != 4:
        return jsonify({"ok": False, "error": "command name must be 4 characters"}), 400
    try:
        payload = bytes.fromhex(hexstr)
    except ValueError:
        return jsonify({"ok": False, "error": "payload must be hex bytes"}), 400
    try:
        acked = _controller().raw(name, payload, via="ui", sender=request.remote_addr)
    except NotConnected as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": acked, "acked": acked})
