"""Event days and earnings days.

Market-wide events come from config/events.json (and trader/events.json if you keep your own copy).
Actions per type (trader/autopilot.json -> event_actions):
  "trade"        trade as usual, only tag the trades (the statistics then show how those days went)
  "skip"         no new entries that day
  "flat_before"  no entries after `event_last_entry` (12:00) and everything flat by `event_flat_by`
                 (13:55), before the Fed's 2:00 PM decision
Earnings: Nasdaq's public earnings calendar lists the stocks reporting today (before the open or
after the close). `earnings_action`: "skip" (default) or "trade" (tag only).
"""
import json
import os
import urllib.request

DEFAULT_ACTIONS = {"fomc": "flat_before", "cpi": "trade", "jobs": "trade"}


def today_event(root, date):
    for path in (os.path.join(root, "trader", "events.json"), os.path.join(root, "config", "events.json")):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        for e in doc.get("events", []):
            if e.get("date") == date:
                return e
    return None


def action_for(event, ap):
    if not event:
        return None
    acts = dict(DEFAULT_ACTIONS, **(ap.get("event_actions") or {}))
    return acts.get(event.get("type"), "trade")


def parse_nasdaq(doc):
    """Nasdaq's earnings calendar JSON -> {SYMBOL: 'pre' | 'after' | 'unknown'}"""
    rows = (((doc or {}).get("data") or {}).get("rows")) or []
    out = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        t = str(r.get("time") or "")
        out[sym] = "pre" if "pre-market" in t else "after" if "after-hours" in t else "unknown"
    return out


def earnings_today(date, timeout=20):
    req = urllib.request.Request("https://api.nasdaq.com/api/calendar/earnings?date=" + date,
                                 headers={"User-Agent": "Mozilla/5.0 (day-trader-autopilot)",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_nasdaq(json.loads(r.read().decode("utf-8", "replace")))
