"""What actually worked: the autopilot's real paper trades, grouped every useful way.

Used by the dashboard (/api/stats) and by tuning/analyze_export.py, so both say the same.
R = the trade's P&L divided by its planned risk (risk a share x shares).
"""
import json
import os
import statistics

MIN_N = 10   # fewer trades than this in a group: shown, but marked as too few to judge


def load(ap_dir):
    """(closed trades, all candidates, learning records) from trader/autopilot/."""
    def rj(p, d):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return d
    trades, cands, seen_days = [], [], set()
    days_dir = os.path.join(ap_dir, "days")
    if os.path.isdir(days_dir):
        for name in sorted(os.listdir(days_dir)):
            if not name.endswith(".json"):
                continue
            day = rj(os.path.join(days_dir, name), {})
            seen_days.add(day.get("date"))
            _absorb(day, trades, cands)
    for h in rj(os.path.join(ap_dir, "history.json"), []) or []:
        if h.get("date") not in seen_days:          # older days, saved before the full day files
            seen_days.add(h.get("date"))
            for t in h.get("trades", []):
                trades.append(dict(t, date=t.get("date") or h["date"]))
    today = rj(os.path.join(ap_dir, "state.json"), {}) or {}
    if today.get("date") and today["date"] not in seen_days:
        _absorb(today, trades, cands)
    closed = [t for t in trades if t.get("pnl") is not None and t.get("status") not in ("rejected", "open", "pending")]
    load.missed = [t for t in trades if t.get("status") == "missed"]
    return closed, cands, rj(os.path.join(ap_dir, "learning.json"), []) or []


def _absorb(day, trades, cands):
    ip = {r.get("symbol") for r in day.get("in_play") or []}
    for t in day.get("trades", []):
        t = dict(t, date=t.get("date") or day.get("date"))
        t.setdefault("in_play", t.get("symbol") in ip)
        trades.append(t)
    for c in (day.get("candidates") or {}).values():
        cands.append(dict(c, date=day.get("date"), in_play=c.get("symbol") in ip))


def summary(ts):
    rs = [t["R"] for t in ts if t.get("R") is not None]
    pnl = [t["pnl"] for t in ts]
    if not ts:
        return {"n": 0}
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(ts), "win_pct": round(100 * len(wins) / len(ts), 1),
            "avg_R": round(statistics.mean(rs), 3) if rs else None,
            "sum_R": round(sum(rs), 2) if rs else None,
            "pnl": round(sum(pnl), 2), "per_trade": round(sum(pnl) / len(ts), 2),
            "avg_win": round(statistics.mean(wins), 2) if wins else None,
            "avg_loss": round(-statistics.mean(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
            "max_drawdown": round(dd, 2), "enough": len(ts) >= MIN_N}


def _bucket_time(t):
    at = t.get("at") or "00:00"
    h, m = [int(x) for x in at.split(":")[:2]]
    mins = h * 60 + m
    start = max(585, mins - (mins - 585) % 30)            # 9:45, 10:15, 10:45, ...
    return "%d:%02d–%d:%02d" % (start // 60, start % 60, (start + 30) // 60, (start + 30) % 60)


def _bucket(v, edges, labels):
    if v is None:
        return "unbekannt"
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


EXIT_NAMES = {"fixed": "festes Ziel", "breakeven": "Stop auf Einstieg", "partial": "halb bei 1R",
              "trail_r": "Trailing R-Stufen", "trail_atr": "Trailing ATR"}
HOW_NAMES = {"target": "Ziel", "stop": "Stop", "trail": "Trailing-Stop", "flat": "Flat 15:55"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _weekday(date):
    import datetime
    try:
        return WEEKDAYS[datetime.date.fromisoformat(date).weekday()]
    except (TypeError, ValueError):
        return "?"


GROUPS = [
    ("Richtung", lambda t: "Short" if t.get("side") == "sell" else "Long"),
    ("Uhrzeit des Einstiegs (New York)", _bucket_time),
    ("Ausstiegsart", lambda t: EXIT_NAMES.get(t.get("exit_mode") or "fixed", t.get("exit_mode"))),
    ("Wie beendet", lambda t: HOW_NAMES.get(t.get("status"), t.get("status"))),
    ("Stock in play", lambda t: "ja" if t.get("in_play") else "nein"),
    ("Setup", lambda t: t.get("setup") or "ORB 15 min"),
    ("Einstiegsorder", lambda t: "Limit" if t.get("entry_order") == "limit" else "Market"),
    ("Ereignistag", lambda t: t.get("event") or "normaler Tag"),
    ("Relatives Volumen", lambda t: _bucket((t.get("features") or {}).get("rvol"), [1.5, 2.5], ["unter 1,5×", "1,5–2,5×", "über 2,5×"])),
    ("Markt (SPY) beim Einstieg", lambda t: _bucket((t.get("features") or {}).get("spy_change"), [0], ["SPY im Minus", "SPY im Plus"])),
    ("Stärke gegenüber SPY", lambda t: _bucket((t.get("features") or {}).get("rs"), [0], ["schwächer (für die Richtung)", "stärker (für die Richtung)"])),
    ("Time-Machine-Edge", lambda t: _bucket((t.get("tm") or {}).get("edge_pts"), [0, 5, 15], ["unter 0", "0–5 Pkt", "5–15 Pkt", "über 15 Pkt"])),
    ("Wochentag", lambda t: _weekday(t.get("date"))),
    ("Plan-Version", lambda t: "v%s" % t.get("strategy_version", 1)),
    ("Aktie", lambda t: t.get("symbol", "?")),
]


def _order(title, r):
    lab = str(r["label"])
    if title == "Aktie":
        return (-r["n"], lab)
    if title.startswith("Uhrzeit"):
        h, m = lab.split("–")[0].split(":")
        return (int(h) * 60 + int(m), lab)
    if title == "Wochentag":
        return (WEEKDAYS.index(lab) if lab in WEEKDAYS else 9, lab)
    if title == "Time-Machine-Edge":
        return (["unter 0", "0–5 Pkt", "5–15 Pkt", "über 15 Pkt", "unbekannt"].index(lab)
                if lab in ("unter 0", "0–5 Pkt", "5–15 Pkt", "über 15 Pkt", "unbekannt") else 9, lab)
    if title == "Relatives Volumen":
        return (["unter 1,5×", "1,5–2,5×", "über 2,5×", "unbekannt"].index(lab)
                if lab in ("unter 1,5×", "1,5–2,5×", "über 2,5×", "unbekannt") else 9, lab)
    return (0, lab)


def grouped(trades):
    out = []
    for title, key in GROUPS:
        groups = {}
        for t in trades:
            groups.setdefault(key(t), []).append(t)
        rows = [dict(summary(v), label=k) for k, v in groups.items()]
        rows.sort(key=lambda r: _order(title, r))
        if title == "Aktie":
            rows = rows[:15]
        out.append({"title": title, "rows": rows})
    return out


def funnel(cands):
    """Where the candidates stopped, and why (top reasons per stage)."""
    stages = ["scanner", "news", "entry", "exit", "timemachine", "budget", "limits", "risk", "broker"]
    stops, reasons = {}, {}
    for c in cands:
        if c.get("result") == "ORDER PLACED":
            stops["Order"] = stops.get("Order", 0) + 1
            continue
        st = next((s for s in stages if (c.get("stages") or {}).get(s, {}).get("ok") is False), None)
        if not st:
            continue
        stops[st] = stops.get(st, 0) + 1
        why = (c.get("why") or "")
        short = why.split(":")[0][:60] if st in ("timemachine", "entry", "budget", "risk") else why[:60]
        reasons.setdefault(st, {}).setdefault(short, 0)
        reasons[st][short] += 1
    return {"candidates": len(cands), "stopped_at": stops,
            "top_reasons": {k: sorted(v.items(), key=lambda x: -x[1])[:4] for k, v in reasons.items()}}


def report(ap_dir):
    trades, cands, learning = load(ap_dir)
    trades.sort(key=lambda t: (t.get("date") or "", t.get("at") or ""))
    last = learning[-1] if learning else {}
    exp = (last.get("current_eval") or {}).get("mean")
    s = summary(trades)
    equity, eq = [], 0.0
    for t in trades:
        eq += t["pnl"]
        equity.append({"date": t.get("date"), "at": t.get("at"), "symbol": t.get("symbol"), "cum": round(eq, 2)})
    missed = getattr(load, "missed", [])
    s["missed_entries"] = len(missed)
    slips = [(t["fill_price"] - t["signal_price"]) / t["plan"]["risk_per_share"] * (1 if t.get("side", "buy") == "buy" else -1)
             for t in trades if t.get("fill_price") and t.get("signal_price") and (t.get("plan") or {}).get("risk_per_share")]
    s["slippage_R"] = round(statistics.mean(slips), 3) if slips else None
    return {"overall": s, "groups": grouped(trades), "funnel": funnel(cands), "equity": equity,
            "days": sorted({t.get("date") for t in trades}),
            "backtest_expectation_R": exp,
            "vs_backtest": None if exp is None or s.get("avg_R") is None else round(s["avg_R"] - exp, 3),
            "min_n": MIN_N}
