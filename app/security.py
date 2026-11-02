"""
HTTP hardening for the web panel and both APIs.

Setter normally lives on a closed show-control network, so everything here
is defence in depth and each layer is optional:

* ALLOWED_NETWORKS   - IP / CIDR allowlist for every HTTP request
* PANEL_PASSWORD     - HTTP Basic auth on the panel and the internal /api/
* API_KEY            - key on the external /api/v1/ (checked in external_api)
* Cross-site request blocking: a request a browser marks as coming from a
  different site (Sec-Fetch-Site: cross-site) is refused, which stops a web
  page in an operator's browser from firing e.g. <img src=".../cmd?c=FTB">
  at the switcher. Control systems never send that header, so they are
  unaffected. The internal mutating endpoints also only accept JSON bodies,
  which an HTML form cannot produce.
* Response headers: strict CSP (no inline script), no framing, no sniffing,
  API responses never cached.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging

from flask import Response, current_app, jsonify, request

log = logging.getLogger("setter.security")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                               "base-uri 'self'; form-action 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# Paths monitoring tools may poll without credentials. They reveal nothing
# beyond "alive" and the connection state.
UNAUTHENTICATED = ("/healthz",)


def parse_networks(spec: str) -> list:
    """'10.1.1.0/24, 192.168.10.5' -> [IPv4Network, ...]. Raises ValueError."""
    nets = []
    for part in (spec or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def address_allowed(addr: str, networks: list) -> bool:
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks)


def constant_time_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _unauthorized():
    resp = jsonify({"ok": False, "error": "authentication required"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = 'Basic realm="Setter"'
    return resp


def _json_error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status


def init_app(app):
    cfg = app.config
    try:
        networks = parse_networks(cfg.get("ALLOWED_NETWORKS", ""))
    except ValueError as exc:
        raise RuntimeError(f"ALLOWED_NETWORKS is not valid: {exc}") from exc
    app.config["ALLOWED_NETWORKS_PARSED"] = networks
    panel_password = cfg.get("PANEL_PASSWORD")
    panel_user = cfg.get("PANEL_USER") or "setter"

    @app.before_request
    def gate():
        # 1. address allowlist, for everything
        if not address_allowed(request.remote_addr or "", networks):
            log.warning("Refused HTTP request from %s (not in ALLOWED_NETWORKS)", request.remote_addr)
            return _json_error(403, "address not allowed")

        # 2. cross-site requests from browsers
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            log.warning("Refused cross-site request from %s to %s", request.remote_addr, request.path)
            return _json_error(403, "cross-site requests are not accepted")

        if request.path in UNAUTHENTICATED:
            return None

        # 3. Basic auth for the panel and the internal API (the external API has API_KEY)
        if panel_password and not request.path.startswith("/api/v1/"):
            auth = request.authorization
            if (auth is None or auth.type != "basic"
                    or not constant_time_equal(auth.username, panel_user)
                    or not constant_time_equal(auth.password, panel_password)):
                return _unauthorized()

        # 4. internal mutating endpoints take JSON only (an HTML form cannot send it)
        if (request.path.startswith("/api/") and not request.path.startswith("/api/v1/")
                and request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.content_length and not request.is_json):
            return _json_error(415, "send a JSON body")
        return None

    @app.after_request
    def headers(resp: Response):
        for name, value in SECURITY_HEADERS.items():
            resp.headers.setdefault(name, value)
        if request.path.startswith("/api/") or request.path in UNAUTHENTICATED:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def api_error(err):
        if request.path.startswith("/api/") or request.path in UNAUTHENTICATED:
            return _json_error(err.code, err.name.lower())
        return err

    @app.errorhandler(500)
    def internal_error(err):
        log.exception("unhandled error in %s %s", request.method, request.path)
        if request.path.startswith("/api/"):
            return _json_error(500, "internal error")
        return err

    @app.get("/healthz")
    def healthz():
        ctl = app.extensions["setter"]
        state = ctl.connection_state()
        return jsonify({"ok": True, "connection": state}), 200

    # Tell the operator what is still open, once, in the service log and the audit log.
    warnings = []
    if not networks:
        warnings.append("ALLOWED_NETWORKS is not set: any address that can reach this port can use the panel")
    if not panel_password:
        warnings.append("PANEL_PASSWORD is not set: the web panel and /api/ need no login")
    if not cfg.get("API_KEY"):
        warnings.append("API_KEY is not set: /api/v1/ accepts commands from anyone who can reach it")
    if cfg.get("SECRET_KEY_WAS_DEFAULT"):
        warnings.append("SECRET_KEY is not set in .env: using a random key for this run")
    if cfg.get("DEBUG"):
        warnings.append("running in development mode (DEBUG on); use FLASK_ENV=production for a show")
    return warnings
