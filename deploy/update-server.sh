#!/usr/bin/env bash
# Pull the newest version from GitHub and restart the service. Your .env and trader/ data stay.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=day-trader-autopilot
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten: sudo bash deploy/update-server.sh"; exit 1; }
OWNER="$(stat -c %U "$APP")"
echo "Neue Version von GitHub holen (bei einem privaten Repository: GitHub-Name und Token eingeben) ..."
sudo -u "$OWNER" git -C "$APP" pull --ff-only
systemctl restart "$SERVICE"
sleep 4
systemctl is-active --quiet "$SERVICE" && echo "Aktualisiert und neu gestartet." \
  || { journalctl -u "$SERVICE" -n 30 --no-pager; exit 1; }
