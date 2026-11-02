#!/usr/bin/env bash
# Production install for Setter.
#
# Creates the virtualenv if needed, installs requirements, writes a systemd
# unit with the right user and paths, then enables and starts it.
#
#   cd ~/Setter && sudo bash install.sh
#
# Safe to re-run after a git pull: it updates requirements and restarts the
# service. Override the HTTP port with PORT=8080 sudo bash install.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${SUDO_USER:-$(whoami)}"
SERVICE=/etc/systemd/system/setter.service
PORT="${PORT:-5000}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash install.sh" >&2
  exit 1
fi

echo "Installing Setter from $APP_DIR as user $APP_USER"

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
sudo -u "$APP_USER" mkdir -p "$APP_DIR/instance"

cat > "$SERVICE" <<UNIT
[Unit]
Description=Setter - ATEM switcher control bridge
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
# One worker only: the switcher session and the UDP listener live in the process.
# --env wins over anything in .env (systemd lets EnvironmentFile override Environment=).
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --threads 4 --env FLASK_ENV=production --bind 0.0.0.0:$PORT "app:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable setter
systemctl restart setter          # starts it, or picks up new code on an update
sleep 1
systemctl --no-pager --lines=5 status setter || true
echo
echo "Setter is running on http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
echo "Logs: sudo journalctl -u setter -f      Audit: journalctl -t setter -f"
