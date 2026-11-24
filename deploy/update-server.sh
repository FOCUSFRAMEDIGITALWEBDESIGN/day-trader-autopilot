#!/usr/bin/env bash
# Update the server installation and restart the service. Your .env and trader/ data stay.
#   sudo bash deploy/update-server.sh                         # from GitHub (git pull), if installed with git clone
#   sudo bash deploy/update-server.sh /root/day-trader-autopilot.zip   # from an uploaded zip
set -euo pipefail
as_user() { local u="$1"; shift; if command -v sudo >/dev/null 2>&1; then sudo -u "$u" "$@"; else runuser -u "$u" -- "$@"; fi; }
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=day-trader-autopilot
[ "$(id -u)" = 0 ] || { echo "Bitte als root starten (oder mit sudo): bash deploy/update-server.sh"; exit 1; }
OWNER="$(stat -c %U "$APP")"
if [ $# -ge 1 ]; then
  ZIP="$1"
  [ -f "$ZIP" ] || { echo "ZIP nicht gefunden: $ZIP"; exit 1; }
  TMP="$(mktemp -d)"
  python3 -m zipfile -e "$ZIP" "$TMP"
  SRC="$TMP"
  [ -d "$TMP/day-trader-autopilot" ] && SRC="$TMP/day-trader-autopilot"
  [ -f "$SRC/autopilot/autopilot.py" ] || { echo "Das ist keine Day-Trader-Autopilot-ZIP."; rm -rf "$TMP"; exit 1; }
  rm -rf "$SRC/trader" "$SRC/.env"                     # never overwrite your data or keys
  cp -a "$SRC/." "$APP/"
  rm -rf "$TMP"
  chown -R "$OWNER":"$OWNER" "$APP"
  echo "Dateien aus $ZIP übernommen (.env und trader/ unverändert)."
else
  [ -d "$APP/.git" ] || { echo "Keine Git-Installation: gib die neue ZIP an, z. B. sudo bash $0 /root/day-trader-autopilot.zip"; exit 1; }
  echo "Neue Version von GitHub holen ..."
  as_user "$OWNER" git -C "$APP" pull --ff-only
fi
systemctl restart "$SERVICE"
sleep 4
systemctl is-active --quiet "$SERVICE" && echo "Aktualisiert und neu gestartet." \
  || { journalctl -u "$SERVICE" -n 30 --no-pager; exit 1; }
