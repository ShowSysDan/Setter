"""
Setter application factory.

Builds the Flask app, loads settings, wires up the audit log, and starts the
switcher connection and UDP listener in background threads. Run it with a
single worker process (see README): the switcher session and the UDP
listener live inside the process, so a second worker would open a second
connection to the switcher and fight over the UDP port.
"""
from __future__ import annotations

import logging
import os

from flask import Flask, request

from . import security
from .__version__ import __version__
from .services.audit_log import AuditLog
from .services.controller import Controller
from .services.settings import Settings


def create_app(config_object=None, autostart: bool | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    if config_object is None:
        from config import get_config
        config_object = get_config()
    app.config.from_object(config_object)
    os.makedirs(app.instance_path, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    settings_file = app.config.get("SETTINGS_FILE") or os.path.join(app.instance_path, "settings.json")
    settings = Settings(settings_file)
    audit = AuditLog(tag="setter")
    controller = Controller(settings, audit)
    controller.configure_syslog()
    app.extensions["setter"] = controller

    from .routes.main import bp as main_bp
    from .routes.api import bp as api_bp
    from .routes.external_api import bp as external_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(external_bp)
    security_warnings = security.init_app(app)

    @app.context_processor
    def inject_globals():
        return {"version": __version__, "settings": settings.all(), "current_path": request.path}

    if autostart is None:
        autostart = app.config.get("SETTER_AUTOSTART", True)
    if autostart:
        controller.start()
    for text in security_warnings:
        audit.warning(text, via="startup")
    if settings.get("udp_enabled") and not settings.get("udp_allowed_senders"):
        audit.warning("UDP control accepts commands from any address; set 'Allowed senders' in Settings", via="startup")

    return app
