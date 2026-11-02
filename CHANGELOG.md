# Changelog

## Unreleased

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
