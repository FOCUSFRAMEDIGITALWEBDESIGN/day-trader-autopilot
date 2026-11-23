#!/usr/bin/env bash
cd "$(dirname "$0")"
for p in autopilot dashboard; do
  f=trader/autopilot/$p.pid; [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && rm -f "$f" && echo "$p gestoppt"
done
