# Changelog

## Unreleased

- Default web panel port is now 5110 (was 5000) for `run.py` and `install.sh`.

- In-app API library at `/reference` (nav: **API**): task cookbook with *Send*
  buttons, command and argument reference with the switcher's current source names,
  and shell / HTTP / Q-SYS / Crestron / Companion snippets pre-filled with this
  Setter's address and port.

Hardening sweep:

- UDP control: allowed-senders list (IPs / CIDRs) in Settings; other senders are
  dropped without a reply and logged (rate limited). Listener re-binds itself if
  its socket breaks instead of dying silently. 2 KB datagram cap.
- HTTP: `ALLOWED_NETWORKS` address allowlist, `PANEL_PASSWORD` / `PANEL_USER` Basic
  auth on the panel and `/api/`, cross-site request blocking via `Sec-Fetch-Site`,
  JSON-only internal mutating endpoints, strict Content-Security-Policy and other
  security headers, 64 KB body limit, JSON error responses on `/api/`, `/healthz`.
- `RAW` is refused from UDP and `/api/v1/` unless enabled in Settings.
- Command limits: 32 commands per message, 200 characters per command.
- Audit log strips control characters and bounds field length (no syslog line forgery).
- Settings: switcher / syslog hosts validated as address or hostname, UDP bind
  address must be a literal IP.
- Constant-time comparison for the API key and panel password.
- Production: random SECRET_KEY if `.env` has the default; dev server runs without
  the Werkzeug debugger console.
- systemd unit sandboxed (NoNewPrivileges, empty capability set, ProtectSystem=strict,
  PrivateTmp/Devices, syscall filter, ...) and `StartLimitIntervalSec=0`; `.env` 600,
  `instance/` 700; `HOST=` override in install.sh; Python version check.
- Dependencies pinned to exact tested versions; Python 3.10+ required.
- Start-up writes a WARNING to the audit log for each access control still open.

- `install.sh` and `setter.service`: pass `FLASK_ENV=production` to gunicorn directly.
  systemd lets `EnvironmentFile=` override `Environment=`, so the `.env` copied from
  `.env.example` was silently running the service in development mode.
- `install.sh`: fills in `FLASK_ENV=production` and a random `SECRET_KEY` when it
  creates `.env`, and restarts the service when re-run after an update.
- Remote syslog "sent" counter is counted on success instead of derived, so the
  Settings page never shows a phantom delivery while a send is in flight.
- README: real clone URL, requirements section, update procedure.

## 0.1.0

First release.

- Pure-Python ATEM UDP client (handshake, acks, retransmits, keepalive, state dump)
- Dashboard with program/preview buses, cut, auto, fade, fade to black, aux routing,
  downstream keys and macro buttons
- Settings page: switcher IP, input / ME / aux / DSK / macro counts, UDP listener
- UDP control listener with a plain-text command protocol and OK / ERR replies
- Same protocol over HTTP at `/api/v1/cmd` for show-control systems that prefer HTTP
- Control string tester and raw ATEM command sender on the dashboard
- Syslog audit log with an activity feed on the dashboard
- Remote syslog target (UDP or TCP, configurable facility) for all actions and any
  application warning or error, with delivery counters and a test button in Settings
- `tools/fake_atem.py` switcher simulator and `tools/udp_send.py` test sender
- systemd install script
