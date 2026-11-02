#!/usr/bin/env bash
# Production install for Setter.
#
# Creates the virtualenv if needed, installs the pinned requirements, writes
# a hardened systemd unit with the right user and paths, then enables and
# (re)starts it.
#
#   cd ~/Setter && sudo bash install.sh
#
# Safe to re-run after a git pull: it updates requirements and restarts the
# service. Options, as environment variables:
#
#   PORT=8080  sudo bash install.sh      HTTP port (default 5110)
#   HOST=10.1.1.5 sudo bash install.sh   bind the web panel to one interface (default 0.0.0.0)

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${SUDO_USER:-$(whoami)}"
SERVICE=/etc/systemd/system/setter.service
PORT="${PORT:-5110}"
HOST="${HOST:-0.0.0.0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash install.sh" >&2
  exit 1
fi
if [ "$APP_USER" = "root" ]; then
  echo "Run as the account the service should use, with sudo (not from a root shell):" >&2
  echo "  sudo bash install.sh" >&2
  exit 1
fi
if ! command -v python3 >/dev/null; then
  echo "python3 is not installed. On Debian / Ubuntu / Raspberry Pi OS:" >&2
  echo "  sudo apt install python3 python3-venv python3-pip" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Setter needs Python 3.10 or newer; found $(python3 --version)" >&2
  exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  echo "The python3-venv package is missing:  sudo apt install python3-venv" >&2
  exit 1
fi
case "$PORT" in ''|*[!0-9]*) echo "PORT must be a number" >&2; exit 1;; esac

echo "Installing Setter from $APP_DIR as user $APP_USER, web panel on $HOST:$PORT"

if [ ! -d "$APP_DIR/venv" ]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sudo -u "$APP_USER" sed -i 's/^FLASK_ENV=.*/FLASK_ENV=production/' "$APP_DIR/.env"
  sudo -u "$APP_USER" sed -i "s/^SECRET_KEY=change-me$/SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')/" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from .env.example with a random SECRET_KEY"
fi
# .env may hold API_KEY / PANEL_PASSWORD; settings.json is written by the service
chmod 600 "$APP_DIR/.env"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/instance"
chmod 700 "$APP_DIR/instance"

# Binding below 1024 needs a capability; everything else runs with none.
CAPS=""
if [ "$PORT" -lt 1024 ]; then
  CAPS="CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE"
else
  CAPS="CapabilityBoundingSet="
fi

cat > "$SERVICE" <<UNIT
[Unit]
Description=Setter - ATEM switcher control bridge
Documentation=file://$APP_DIR/README.md
After=network-online.target
Wants=network-online.target
# Never give up restarting: this has to come back on its own during a show.
StartLimitIntervalSec=0

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
# One worker only: the switcher session and the UDP listener live in the process.
# --env wins over anything in .env (systemd lets EnvironmentFile override Environment=).
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --threads 4 --timeout 30 --graceful-timeout 10 \\
    --env FLASK_ENV=production --bind $HOST:$PORT "app:create_app()"
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=3
TimeoutStopSec=15
UMask=0077
# The checkout is read-only to the service, so do not try to write .pyc files into it
Environment=PYTHONDONTWRITEBYTECODE=1

# --- sandboxing: the process only needs the network, its own folder and /dev/log
NoNewPrivileges=true
$CAPS
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$APP_DIR/instance
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable setter
systemctl restart setter          # starts it, or picks up new code on an update
sleep 2
if ! systemctl is-active --quiet setter; then
  echo "Setter did not start. Last log lines:" >&2
  journalctl -u setter --no-pager --lines=20 >&2 || true
  exit 1
fi
systemctl --no-pager --lines=3 status setter || true
echo
if [ "$HOST" = "0.0.0.0" ]; then
  echo "Setter is running on http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
else
  echo "Setter is running on http://$HOST:$PORT"
fi
echo "Logs: sudo journalctl -u setter -f      Audit: journalctl -t setter -f"
if ! grep -qE '^(PANEL_PASSWORD|ALLOWED_NETWORKS|API_KEY)=' "$APP_DIR/.env"; then
  echo
  echo "Note: $APP_DIR/.env sets no PANEL_PASSWORD, ALLOWED_NETWORKS or API_KEY,"
  echo "so anyone who can reach port $PORT can drive the switcher. See README 'Security'."
fi
