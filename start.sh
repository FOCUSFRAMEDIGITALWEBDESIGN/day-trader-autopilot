#!/usr/bin/env bash
# Starts the autopilot (paper only) and the dashboard. Logs go to trader/autopilot/*.log
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p trader/autopilot
[ -f trader/config.json ] || cp config/config.json trader/config.json
[ -f trader/autopilot.json ] || cp config/autopilot.json trader/autopilot.json
set -a; [ -f .env ] && . ./.env; set +a
HOST="${DASHBOARD_HOST:-127.0.0.1}"
nohup python3 autopilot/autopilot.py run >> trader/autopilot/autopilot.log 2>&1 &
echo $! > trader/autopilot/autopilot.pid
nohup python3 dashboard/server.py --host "$HOST" --port "${DASHBOARD_PORT:-8765}" >> trader/autopilot/dashboard.log 2>&1 &
echo $! > trader/autopilot/dashboard.pid
echo "Autopilot läuft (PID $(cat trader/autopilot/autopilot.pid)). Dashboard: http://$HOST:${DASHBOARD_PORT:-8765}"
