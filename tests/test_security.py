"""
HTTP and UDP hardening: address allowlists, panel password, API key,
cross-site blocking, JSON-only mutations, security headers, log sanitising.
Run with: python -m unittest
"""
import base64
import json
import os
import socket
import tempfile
import time
import unittest

from app import create_app
from app.services.audit_log import AuditLog, _clean
from app.services.settings import Settings, SettingsError, parse_networks
from app.services.udp_listener import UdpListener


def make_app(**overrides):
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "settings.json"), "w") as fh:
        json.dump({"auto_connect": False, "udp_enabled": False}, fh)

    class Cfg:
        TESTING = True
        SECRET_KEY = "test"
        LOG_LEVEL = "WARNING"
        API_KEY = None
        PANEL_USER = "setter"
        PANEL_PASSWORD = None
        ALLOWED_NETWORKS = ""
        MAX_CONTENT_LENGTH = 64 * 1024
        SETTER_AUTOSTART = False
        SETTINGS_FILE = os.path.join(tmp, "settings.json")

    for k, v in overrides.items():
        setattr(Cfg, k, v)
    return create_app(Cfg, autostart=False)


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class HttpHardening(unittest.TestCase):
    def test_security_headers_and_no_inline_script(self):
        c = make_app().test_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        html = r.get_data(as_text=True)
        # the only <script> tags are the external file and the JSON data block
        import re
        for tag in re.findall(r"<script[^>]*>", html):
            self.assertTrue('src=' in tag or 'type="application/json"' in tag, tag)
        r = c.get("/api/status")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        r = c.get("/settings")
        self.assertEqual(r.status_code, 200)
        r = c.get("/reference", base_url="http://10.1.1.5:5000")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('data-send="FADE 2 1s"', html)
        self.assertIn("10.1.1.5:9990", html)                  # UDP target from this Setter's address
        self.assertIn("http://10.1.1.5:5000/api/v1/cmd", html)
        self.assertNotIn("<script>", html)                    # CSP: nothing inline

    def test_cross_site_requests_refused(self):
        c = make_app().test_client()
        r = c.get("/api/v1/cmd?c=PING", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/command", json={"text": "PING"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        for ok in ("same-origin", "same-site", "none"):
            r = c.get("/api/v1/cmd?c=PING", headers={"Sec-Fetch-Site": ok})
            self.assertEqual(r.status_code, 200, ok)
        r = c.get("/api/v1/cmd?c=PING")   # a control system sends no such header
        self.assertEqual(r.status_code, 200)

    def test_internal_api_is_json_only(self):
        c = make_app().test_client()
        r = c.post("/api/command", data={"text": "FTB"})          # what an HTML form would send
        self.assertEqual(r.status_code, 415)
        r = c.post("/api/settings", data={"input_count": "4"})
        self.assertEqual(r.status_code, 415)
        r = c.post("/api/command", json={"text": "PING"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["replies"], ["PONG"])
        r = c.put("/api/settings", json=["not", "an", "object"])
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/command", json={"text": ["PING"]})
        self.assertEqual(r.status_code, 400)

    def test_panel_password(self):
        c = make_app(PANEL_PASSWORD="hunter2").test_client()
        for path in ("/", "/settings", "/api/status"):
            r = c.get(path)
            self.assertEqual(r.status_code, 401, path)
            self.assertIn("Basic", r.headers["WWW-Authenticate"])
        self.assertEqual(c.get("/api/status", headers=basic("setter", "wrong")).status_code, 401)
        self.assertEqual(c.get("/api/status", headers=basic("nobody", "hunter2")).status_code, 401)
        self.assertEqual(c.get("/api/status", headers=basic("setter", "hunter2")).status_code, 200)
        self.assertEqual(c.get("/", headers=basic("setter", "hunter2")).status_code, 200)
        # the external API is governed by API_KEY, not the panel password
        self.assertEqual(c.get("/api/v1/cmd?c=PING").status_code, 200)
        # health check stays open for monitoring
        r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["connection"], "disabled")

    def test_api_key(self):
        c = make_app(API_KEY="s3cret").test_client()
        self.assertEqual(c.get("/api/v1/cmd?c=PING").status_code, 401)
        self.assertEqual(c.get("/api/v1/cmd?c=PING&api_key=nope").status_code, 401)
        self.assertEqual(c.get("/api/v1/cmd?c=PING&api_key=s3cret").status_code, 200)
        self.assertEqual(c.get("/api/v1/status", headers={"X-API-Key": "s3cret"}).status_code, 200)

    def test_allowed_networks(self):
        app = make_app(ALLOWED_NETWORKS="10.1.1.0/24, 192.168.10.5")
        c = app.test_client()
        r = c.get("/api/status", environ_base={"REMOTE_ADDR": "10.1.1.20"})
        self.assertEqual(r.status_code, 200)
        r = c.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.10.5"})
        self.assertEqual(r.status_code, 200)
        r = c.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.10.6"})
        self.assertEqual(r.status_code, 403)
        r = c.get("/healthz", environ_base={"REMOTE_ADDR": "172.16.0.1"})
        self.assertEqual(r.status_code, 403)
        with self.assertRaises(RuntimeError):
            make_app(ALLOWED_NETWORKS="not-a-network")

    def test_body_size_limit(self):
        c = make_app().test_client()
        r = c.post("/api/command", data=b"x" * (70 * 1024), content_type="application/json")
        self.assertEqual(r.status_code, 413)
        self.assertFalse(r.get_json()["ok"])

    def test_raw_gated_on_external_api(self):
        app = make_app()
        c = app.test_client()
        r = c.get("/api/v1/cmd?c=RAW+CPgI+00000001")
        self.assertEqual(r.status_code, 422)
        self.assertIn("RAW is only accepted", r.get_json()["replies"][0])


class UdpAllowlist(unittest.TestCase):
    def test_senders_outside_list_are_dropped(self):
        calls = []
        refused = []
        lst = UdpListener("127.0.0.1", 0, lambda text, addr: calls.append(text) or ["OK"],
                          allowed=parse_networks("10.0.0.0/8"), on_refused=refused.append)
        lst.start()
        try:
            port = lst._sock.getsockname()[1]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.sendto(b"PING", ("127.0.0.1", port))
            with self.assertRaises(socket.timeout):     # no reply to an unknown sender
                s.recvfrom(100)
            self.assertEqual(calls, [])
            self.assertEqual(lst.refused, 1)
            self.assertEqual(len(refused), 1)
            s.sendto(b"PING", ("127.0.0.1", port))      # second one within a minute is not re-logged
            time.sleep(0.1)
            self.assertEqual(lst.refused, 2)
            self.assertEqual(len(refused), 1)
            self.assertTrue(lst.sender_allowed("10.1.1.1"))
            self.assertFalse(lst.sender_allowed("127.0.0.1"))
            self.assertFalse(lst.sender_allowed("garbage"))
            s.close()
        finally:
            lst.stop()

    def test_empty_list_allows_everyone(self):
        lst = UdpListener("127.0.0.1", 0, lambda text, addr: ["PONG"])
        lst.start()
        try:
            port = lst._sock.getsockname()[1]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.sendto(b"PING", ("127.0.0.1", port))
            self.assertEqual(s.recvfrom(100)[0], b"PONG\n")
            s.close()
        finally:
            lst.stop()


class SettingsValidation(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "settings.json")
        self.settings = Settings(self.path)

    def test_allowed_senders_normalised(self):
        changed = self.settings.update({"udp_allowed_senders": "10.1.1.20 192.168.10.0/24;10.2.0.0/16"})
        self.assertEqual(changed["udp_allowed_senders"], "10.1.1.20/32, 192.168.10.0/24, 10.2.0.0/16")
        with self.assertRaises(SettingsError):
            self.settings.update({"udp_allowed_senders": "10.1.1.999"})
        self.settings.update({"udp_allowed_senders": ""})
        self.assertEqual(self.settings.get("udp_allowed_senders"), "")

    def test_hosts_validated(self):
        for bad in ("", "not a host", "10.1.1.1; rm -rf /", "x" * 300):
            with self.assertRaises(SettingsError, msg=bad):
                self.settings.update({"switcher_ip": bad})
        for good in ("192.168.10.240", "atem.venue.local", "atem"):
            self.settings.update({"switcher_ip": good})
        with self.assertRaises(SettingsError):
            self.settings.update({"udp_host": "atem.venue.local"})     # must be a literal address to bind
        with self.assertRaises(SettingsError):
            self.settings.update({"syslog_host": "syslog server"})
        self.settings.update({"syslog_host": "syslog.venue.local"})
        self.assertFalse(self.settings.get("raw_external"))

    def test_unknown_or_bad_values_rejected(self):
        with self.assertRaises(SettingsError):
            self.settings.update({"evil": 1})
        with self.assertRaises(SettingsError):
            self.settings.update({"input_count": 999})


class LogSanitising(unittest.TestCase):
    def test_control_characters_and_length(self):
        self.assertEqual(_clean("FADE 2\r\n<34>forged line"), "FADE 2  <34>forged line")
        self.assertEqual(_clean("a\x00b\x1bc"), "a b c")
        self.assertEqual(_clean(5), 5)
        self.assertTrue(_clean("x" * 1000).endswith("...(truncated)"))
        audit = AuditLog(tag="settertest")
        try:
            audit.info("UDP control string received", text="PING\nINJECTED", sender="1.2.3.4:5")
            entry = audit.since()[-1]
            self.assertNotIn("\n", entry["fields"]["text"])
        finally:
            audit.close()


if __name__ == "__main__":
    unittest.main()
