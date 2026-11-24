#!/usr/bin/env bash
# Day Trader Autopilot on a Linux cloud server (Ubuntu / Debian), next to whatever else runs there.
#
#   sudo bash deploy/install-server.sh                                # dashboard on http://SERVER-IP:8765
#   sudo bash deploy/install-server.sh --domain trading.example.de    # HTTPS via Caddy (ports 80/443 must be free)
#   sudo bash deploy/install-server.sh --port 8877                    # another dashboard port
#
# What it does (it never touches other services):
#   - a system user "daytrader" that owns only this folder
#   - .env with your Alpaca PAPER keys (asked here, typed on the server, never shown) and a random
#     dashboard password (DASHBOARD_TOKEN)
#   - the $2,000 account profile on the first install
#   - a systemd service "day-trader-autopilot": starts at boot, restarts after crashes
#   - with --domain: Caddy as an HTTPS front (only if ports 80 and 443 are free; otherwise it prints
#     the snippet for your existing web server)
# Update later:  sudo bash deploy/update-server.sh
set -euo pipefail

PORT=8765
DOMAIN=""
SERVICE=day-trader-autopilot
RUN_USER=daytrader
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    *) echo "unbekannte Option: $1"; exit 2 ;;
  esac
done

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten: sudo bash deploy/install-server.sh"; exit 1; }
[ -f "$APP/autopilot/autopilot.py" ] || { echo "Das Skript muss im Projektordner liegen ($APP)."; exit 1; }

say "1/6 Python prüfen"
if ! command -v python3 >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || { echo "python3 fehlt und apt-get gibt es nicht: bitte Python 3.9+ installieren."; exit 1; }
  apt-get update -qq && apt-get install -y -qq python3
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "Python 3.9 oder neuer nötig, gefunden: $(python3 --version)"; exit 1; }
PY="$(command -v python3)"
echo "$(python3 --version) in $PY"

say "2/6 Port $PORT prüfen"
if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$PORT )" | grep -q LISTEN; then
  if ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    echo "Port $PORT ist schon belegt (vielleicht dein anderer Bot). Nimm einen anderen: --port 8877"; exit 1
  fi
fi
echo "frei (oder schon von diesem Bot benutzt)"

say "3/6 Benutzer '$RUN_USER' und Rechte"
id "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p "$APP/trader/autopilot"

say "4/6 Schlüssel (.env)"
if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
fi
getv() { grep -E "^$1=" "$APP/.env" | tail -1 | cut -d= -f2-; }
setv() {  # replace or append, without ever printing the value
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  grep -vE "^$k=" "$APP/.env" > "$tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$tmp"
  cat "$tmp" > "$APP/.env"; rm -f "$tmp"
}
if [ -z "$(getv ALPACA_API_KEY)" ] || [ -z "$(getv ALPACA_SECRET_KEY)" ]; then
  echo "Alpaca PAPER-Schlüssel (Alpaca → Paper Trading → API Keys). Die Eingabe bleibt unsichtbar."
  read -r -p "  ALPACA_API_KEY (PK...): " K
  read -r -s -p "  ALPACA_SECRET_KEY: " S; echo
  [ -n "$K" ] && [ -n "$S" ] || { echo "Schlüssel fehlen, Abbruch."; exit 1; }
  setv ALPACA_API_KEY "$K"; setv ALPACA_SECRET_KEY "$S"
fi
setv ALPACA_PAPER_TRADE true
TOKEN="$(getv DASHBOARD_TOKEN)"
if [ ${#TOKEN} -lt 16 ]; then   # a random dashboard password, 32 characters
  setv DASHBOARD_TOKEN "$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  TOKEN="$(getv DASHBOARD_TOKEN)"
fi
if [ -n "$DOMAIN" ]; then setv DASHBOARD_HOST 127.0.0.1; else setv DASHBOARD_HOST 0.0.0.0; fi
setv PORT "$PORT"
chmod 600 "$APP/.env"
chown -R "$RUN_USER":"$RUN_USER" "$APP"

if [ ! -f "$APP/trader/autopilot/account.json" ]; then
  sudo -u "$RUN_USER" "$PY" "$APP/autopilot/autopilot.py" profile 2000 >/dev/null
  echo "Konto-Profil: \$2.000 (ändern: sudo -u $RUN_USER python3 autopilot/autopilot.py profile BETRAG)"
fi

say "5/6 Dienst '$SERVICE'"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Day Trader Autopilot (Alpaca PAPER) with dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$PY deploy/run_all.py
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1 PYTHONUTF8=1
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$APP

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 4
systemctl is-active --quiet "$SERVICE" || { journalctl -u "$SERVICE" -n 30 --no-pager; echo "Der Dienst läuft nicht, siehe oben."; exit 1; }
echo "läuft"

say "6/6 Dashboard erreichbar machen"
URL=""
if [ -n "$DOMAIN" ]; then
  if ss -ltn | grep -qE ':(80|443)\s' && ! systemctl is-active --quiet caddy 2>/dev/null; then
    echo "Ports 80/443 sind schon belegt (ein anderer Webserver). Trag dort diese Weiterleitung ein:"
    echo "  nginx:  location / { proxy_pass http://127.0.0.1:$PORT; proxy_set_header Host \$host; }"
    echo "  Caddy:  $DOMAIN { reverse_proxy 127.0.0.1:$PORT }"
  else
    command -v caddy >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq caddy; }
    touch /etc/caddy/Caddyfile
    if ! grep -q "^$DOMAIN " /etc/caddy/Caddyfile; then
      printf '\n%s {\n    reverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT" >> /etc/caddy/Caddyfile
    fi
    systemctl enable --now caddy && systemctl reload caddy
    echo "HTTPS über Caddy eingerichtet. Der DNS-Eintrag (A-Record) von $DOMAIN muss auf diesen Server zeigen."
  fi
  URL="https://$DOMAIN/?token=$TOKEN"
else
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null && echo "Firewall (ufw): Port $PORT geöffnet"
  fi
  IP="$(curl -fsS -4 --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
  URL="http://$IP:$PORT/?token=$TOKEN"
  echo "Hinweis: ohne Domain läuft das Dashboard unverschlüsselt (http). Mit --domain gibt es HTTPS."
  echo "Prüfe auch die Firewall im one.com-Kundenbereich: Port $PORT muss eingehend erlaubt sein."
fi

say "Fertig"
echo "Dashboard:  $URL"
echo "            (diesen Link als Lesezeichen speichern; er enthält dein Dashboard-Passwort)"
echo "Logs:       journalctl -u $SERVICE -f"
echo "Status:     systemctl status $SERVICE"
echo "Stoppen:    sudo systemctl stop $SERVICE"
echo "Update:     sudo bash $APP/deploy/update-server.sh"
echo "Wichtig:    den Bot auf dem PC beenden (stop.bat), damit nicht zwei Bots dasselbe Konto handeln."
