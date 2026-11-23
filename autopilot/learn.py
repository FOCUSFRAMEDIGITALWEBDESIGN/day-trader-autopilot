#!/usr/bin/env python3
"""The learner: re-tunes the autopilot's strategy once a week, with guard rails.

    python3 autopilot/learn.py               # fetch fresh data, evaluate, maybe switch
    python3 autopilot/learn.py --no-fetch    # use the data already in tuning/data
    python3 autopilot/learn.py --dry-run     # evaluate and report, change nothing

What it does
  1. Downloads the last 60 days of 5-minute bars for the scan list (Yahoo, polite).
  2. Stage 1: screens a fixed grid of strategy settings with the plain rule on the
     older sessions (in-sample) and keeps the best few that also hold up on the
     newer sessions (out-of-sample).
  3. Stage 2: runs those few, plus the CURRENT setting, walk-forward with the Time
     Machine gate exactly as the autopilot trades (only data before each day).
  4. Switches only when the best one is clearly better: its lower confidence bound
     (mean R minus one standard error) beats the current one's by MIN_GAIN_R, its
     mean is positive, and it has at least MIN_TRADES trades. Otherwise it keeps the
     current plan. Every run is logged in trader/autopilot/learning.json.
  5. Live check: if the paper trades of the current plan (at least LIVE_MIN_TRADES)
     average worse than LIVE_STOP_R, it pauses new entries and says why.

What it never touches: budget, max loss per trade, daily loss limit, trades per day,
flat-by time, paper mode. It only changes how setups are chosen, never how much is risked.
"""
import argparse, datetime, glob, gzip, itertools, json, os, statistics, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tuning"))
import backtest as bt  # noqa: E402

tm = bt.tm
T = os.path.join(ROOT, "trader")
AP = os.path.join(T, "autopilot")
DATA = os.path.join(ROOT, "tuning", "data")

MIN_TRADES = 25          # stage-2 trades a candidate needs
MIN_GAIN_R = 0.03        # lower-bound improvement needed to switch
LIVE_MIN_TRADES = 15
LIVE_STOP_R = -0.30
STAGE2_KEEP = 6
REPLAYS = 600

# the only keys the learner may change
GRID = {
    "or_minutes": [15, 30],
    "stop": ["or-mid", "or-low"],
    "target": ["1.5R", "2R", "3R"],
    "last_entry": ["10:30", "11:30", "12:30"],
    "max_pct_over": [0.5, 0.75, 0],
    "min_volume_x": [None, 1.0],
    "gap_filter": [True, False],
}
EDGES = [0.0, 2.0, 5.0]   # tm_min_edge_pts candidates (stage 2)


def rj(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def wj(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, default=str)
    os.replace(tmp, p)


def hm(t):
    h, m = str(t).split(":")[:2]
    return int(h) * 60 + int(m)


def to_bt(s):
    """autopilot settings -> backtest params."""
    return {"or_minutes": int(s["or_minutes"]), "stop": s["stop"].replace("-", "_"),
            "target_r": float(str(s["target"]).rstrip("Rr")),
            "last_entry_slot": (hm(s["last_entry"]) - 570) // 5 - 1,
            "max_pct_over": float(s.get("max_pct_over") or 0), "min_volume_x": float(s.get("min_volume_x") or 0),
            "gap_filter": bool(s.get("gap_filter")), "min_risk_pct": float(s.get("min_risk_pct", 0.15)),
            "max_risk_pct": float(s.get("max_risk_pct", 2.5))}


def key(s):
    return json.dumps({k: s.get(k) for k in GRID}, sort_keys=True)


def label(s, edge=None):
    return "OR%s %s %s bis %s, ≤%s%% über Range, Vol≥%s, Gap %s%s" % (
        s["or_minutes"], s["stop"], s["target"], s["last_entry"], s.get("max_pct_over") or "–",
        s.get("min_volume_x") or "–", "an" if s.get("gap_filter") else "aus",
        "" if edge is None else ", TM-Edge ≥ %g" % edge)


# ---------------------------------------------------------------- data
def fetch(symbols, log):
    os.makedirs(DATA, exist_ok=True)
    import urllib.request
    ok = 0
    for s in symbols:
        p = os.path.join(DATA, "%s-60d5m.json.gz" % s)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < 20 * 3600:
            ok += 1
            continue
        for host in ("query2", "query1", "query2"):
            try:
                req = urllib.request.Request(
                    "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=5m&range=60d" % (host, s),
                    headers={"User-Agent": "day-trader-skill/2.0"})
                raw = urllib.request.urlopen(req, timeout=30).read()
                with gzip.open(p, "wb") as f:
                    f.write(raw)
                ok += 1
                break
            except Exception:
                time.sleep(6)
        time.sleep(1.2)
    log("Daten: %d von %d Aktien aktuell" % (ok, len(symbols)))


def load(symbols):
    data = {}
    for s in symbols:
        p = os.path.join(DATA, "%s-60d5m.json.gz" % s)
        if not os.path.exists(p):
            continue
        try:
            S = bt.sessions_of(p)
        except Exception:
            continue
        if len(S) >= 40:
            data[s] = S
    return data


# ---------------------------------------------------------------- evaluation
def rule_R(data, p, dates_from=None, dates_to=None):
    out = []
    for S in data.values():
        prev = None
        for d, D in S:
            r = bt.trade(D, prev, p)
            prev = D[-1]["c"]
            if r is None or (dates_from and d < dates_from) or (dates_to and d >= dates_to):
                continue
            out.append(r)
    return out


def gated(data, s, recent_from):
    """Walk-forward with the Time Machine: returns {edge: [R...]} for each EDGES value."""
    import gate_eval as ge
    p = to_bt(s)
    p["stop"] = p["stop"]
    rule = {"setup": "orb", "or_minutes": p["or_minutes"], "stop_rule": p["stop"], "target_r": p["target_r"]}
    res = {e: [] for e in EDGES}
    for S in data.values():
        for sig in ge.signals(S, p):
            if sig["date"] < recent_from:
                continue
            past = S[max(0, sig["idx"] - 59):sig["idx"]]
            if len(past) < 20:
                continue
            mc = tm.simulate(tm.build_pool(past), sig["entry"], sig["stop"], sig["target"], sig["k"], bt.FLAT_SLOT,
                             replays=REPLAYS, seed=19)
            sided = (mc["p_target"] + mc["p_stop"]) or 1e-9
            edge = (mc["p_target"] / sided - 1 / (1 + p["target_r"])) * 100
            if mc["avg_R"] < 0:
                continue
            for e in EDGES:
                if edge >= e:
                    res[e].append(sig["R"])
    return res


def summary(rs):
    if not rs:
        return {"n": 0, "mean": None, "lcb": None}
    m = statistics.mean(rs)
    se = statistics.stdev(rs) / len(rs) ** 0.5 if len(rs) > 1 else 1.0
    return {"n": len(rs), "mean": round(m, 4), "lcb": round(m - se, 4),
            "win_pct": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1)}


# ---------------------------------------------------------------- live check
def live_trades(version):
    rows = []
    for h in rj(os.path.join(AP, "history.json"), []) or []:
        rows += h.get("trades", [])
    rows += (rj(os.path.join(AP, "state.json"), {}) or {}).get("trades", [])
    return [t["R"] for t in rows if t.get("R") is not None and t.get("strategy_version", 0) == version]


# ---------------------------------------------------------------- main
def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--no-fetch", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a = a.parse_args(argv)
    t0 = time.time()
    lines = []

    def log(msg):
        lines.append(msg)
        print(msg, flush=True)

    ap_path = os.path.join(T, "autopilot.json")
    cfg = rj(ap_path) or rj(os.path.join(ROOT, "config", "autopilot.json"), {})
    version = int(cfg.get("strategy_version", 1))
    cur = {k: cfg.get(k) for k in GRID}
    for k, v in (("or_minutes", 15), ("stop", "or-mid"), ("target", "2R"), ("last_entry", "11:30"),
                 ("max_pct_over", 0.75), ("min_volume_x", None), ("gap_filter", True)):
        if cur.get(k) is None and k != "min_volume_x":
            cur[k] = v
    cur_edge = float(cfg.get("tm_min_edge_pts", 0.0))
    symbols = [s.upper() for s in cfg.get("symbols", [])] or open(os.path.join(ROOT, "tuning", "universe.txt")).read().split()

    record = {"run_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "strategy_version": version,
              "current": label(cur, cur_edge), "changed": False}

    # live check first: it does not need any data
    live = summary(live_trades(version))
    record["live"] = live
    if live["n"] >= LIVE_MIN_TRADES and live["mean"] < LIVE_STOP_R and not a.dry_run:
        with open(os.path.join(AP, "PAUSE"), "w", encoding="utf-8") as f:
            f.write("paused by the learner: the live plan averaged %.2f R over %d paper trades\n" % (live["mean"], live["n"]))
        log("LIVE-SCHUTZ: aktueller Plan im Paper-Handel Ø %.2f R über %d Trades → neue Einstiege pausiert"
            % (live["mean"], live["n"]))
        record["live_pause"] = True

    if not a.no_fetch:
        fetch(symbols, log)
    data = load(symbols)
    dates = sorted({d for S in data.values() for d, _ in S})
    if len(data) < 15 or len(dates) < 45:
        log("Zu wenig Daten (%d Aktien, %d Tage): nichts geändert" % (len(data), len(dates)))
        record.update(result="zu wenig Daten", log=lines)
        return finish(record, a)
    split = dates[len(dates) * 2 // 3]       # stage 1: older two thirds vs newest third
    recent = dates[len(dates) // 3]          # stage 2: walk-forward on the newer two thirds
    log("%d Aktien, %d Handelstage (%s bis %s)" % (len(data), len(dates), dates[0], dates[-1]))

    # stage 1
    cands = []
    for combo in itertools.product(*(GRID[k] for k in GRID)):
        s = dict(zip(GRID, combo))
        if hm(s["last_entry"]) < 570 + s["or_minutes"] + 15:
            continue
        p = to_bt(s)
        ins = summary(rule_R(data, p, dates_to=split))
        oos = summary(rule_R(data, p, dates_from=split))
        if ins["n"] < 60 or oos["n"] < 25:
            continue
        cands.append((ins["mean"] + min(oos["mean"], ins["mean"] + 0.1), s, ins, oos))
    cands.sort(key=lambda x: -x[0])
    short = [c[1] for c in cands[:STAGE2_KEEP]]
    if key(cur) not in {key(s) for s in short}:
        short.append(cur)
    log("Stufe 1: %d Varianten geprüft, %d in die engere Wahl" % (len(cands), len(short)))

    # stage 2
    results = []
    for s in short:
        res = gated(data, s, recent)
        for e in EDGES:
            sm = summary(res[e])
            results.append({"settings": s, "edge": e, "label": label(s, e), **sm,
                            "current": key(s) == key(cur) and e == cur_edge})
    results = [r for r in results if r["n"] > 0]
    results.sort(key=lambda r: -(r["lcb"] if r["n"] >= MIN_TRADES else -9))
    cur_r = next((r for r in results if r["current"]), {"n": 0, "mean": None, "lcb": None})
    best = next((r for r in results if r["n"] >= MIN_TRADES), None)
    record["top"] = [{k: r[k] for k in ("label", "n", "mean", "lcb", "win_pct", "current")} for r in results[:8]]
    record["current_eval"] = {k: cur_r.get(k) for k in ("n", "mean", "lcb", "win_pct")}
    log("Aktueller Plan (walk-forward): n=%s, Ø %s R" % (cur_r.get("n"), cur_r.get("mean")))

    switch = bool(best and best["mean"] and best["mean"] > 0 and not best["current"] and
                  (cur_r.get("lcb") is None or cur_r["n"] < MIN_TRADES or best["lcb"] >= cur_r["lcb"] + MIN_GAIN_R))
    if switch:
        record.update(changed=True, new=best["label"], new_eval={k: best[k] for k in ("n", "mean", "lcb", "win_pct")})
        log("NEUER PLAN: %s (n=%d, Ø %+.3f R, untere Schranke %+.3f R)" % (best["label"], best["n"], best["mean"], best["lcb"]))
        if not a.dry_run:
            new = rj(ap_path) or dict(cfg)
            new.update(best["settings"])
            new["tm_min_edge_pts"] = best["edge"]
            new["strategy_version"] = version + 1
            new["_note"] = "Plan v%d, vom Lernmodul gesetzt am %s" % (version + 1, record["run_at"])
            wj(ap_path, new)
        record["result"] = "gewechselt auf v%d" % (version + 1)
    else:
        why = "kein Kandidat mit genug Trades" if not best else \
              "aktueller Plan ist schon der beste" if best["current"] else \
              "bester Kandidat ist nicht positiv" if not (best["mean"] and best["mean"] > 0) else \
              "Vorsprung zu klein (%.3f R, nötig %.3f)" % (best["lcb"] - (cur_r.get("lcb") or 0), MIN_GAIN_R)
        log("Plan bleibt: %s" % why)
        record["result"] = "behalten: " + why
    record["seconds"] = round(time.time() - t0)
    record["log"] = lines
    return finish(record, a)


def finish(record, a):
    if not a.dry_run:
        hist = rj(os.path.join(AP, "learning.json"), []) or []
        hist.append(record)
        wj(os.path.join(AP, "learning.json"), hist[-60:])
        with open(os.path.join(AP, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "level": "trade" if record.get("changed") else "info",
                                "msg": "Lernen: " + record.get("result", "")}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
