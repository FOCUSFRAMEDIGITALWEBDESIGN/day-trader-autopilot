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
import research as rsch  # noqa: E402

tm = bt.tm
T = os.path.join(ROOT, "trader")
AP = os.path.join(T, "autopilot")
DATA = os.path.join(ROOT, "tuning", "data")

MIN_TRADES = 25          # stage-2 trades a candidate needs (x3 on Alpaca's years)
MIN_TRADES_SHORT_DATA = 40   # ... and on Yahoo's 60 days
MIN_GAIN_R = 0.03        # lower-bound improvement needed to switch
LIVE_MIN_TRADES = 15
LIVE_STOP_R = -0.30
STAGE2_KEEP = 6
REPLAYS = 600

# the only keys the learner may change (how setups are chosen and exited, never how much is risked)
KEYS = ["sides", "or_minutes", "last_entry", "max_pct_over", "gap_filter", "rvol_min", "market_filter", "rs_min",
        "stop", "target", "exit_mode", "exit_atr_k", "min_volume_x"]
GRID = {k: None for k in KEYS}
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


def fetch_yahoo(symbols, log):
    os.makedirs(DATA, exist_ok=True)
    import urllib.request
    ok = 0
    for s in symbols:
        p = os.path.join(DATA, "%s-60d5m.json.gz" % s)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < 20 * 3600 and os.path.getsize(p) > 5000:
            ok += 1
            continue
        for host in ("query2", "query1", "query2"):
            try:
                req = urllib.request.Request(
                    "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=5m&range=60d" % (host, s),
                    headers={"User-Agent": "day-trader-skill/2.0"})
                raw = urllib.request.urlopen(req, timeout=30).read()
                if len(raw) < 5000:
                    raise ValueError("empty answer")
                with gzip.open(p, "wb") as f:
                    f.write(raw)
                ok += 1
                break
            except Exception:
                time.sleep(8)
        time.sleep(1.2)
    log("Yahoo (60 Tage): %d von %d Aktien aktuell" % (ok, len(symbols)))


def fetch(symbols, years, log):
    """Alpaca's years first (needs the paper keys), Yahoo's 60 days as the fallback."""
    try:
        import alpaca_data
        if alpaca_data.fetch_history(symbols, years, log) >= 15:
            return
    except Exception as e:
        log("Alpaca-Historie nicht verfügbar (%s): nutze Yahoo" % str(e)[:100])
    fetch_yahoo(symbols, log)


def summary(rs):
    return rsch.summary(rs)


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
    cur = {"sides": "both", "or_minutes": 15, "last_entry": "11:30", "max_pct_over": 0.75, "gap_filter": True, "rvol_min": 0,
           "market_filter": "none", "rs_min": None, "stop": "or-mid", "target": "2R", "exit_mode": "fixed",
           "min_volume_x": None}
    cur.update({k: cfg[k] for k in KEYS if k in cfg})
    cur["min_volume_x"] = None
    cur_edge = float(cfg.get("tm_min_edge_pts", 0.0))
    symbols = [s.upper() for s in cfg.get("symbols", [])] or open(os.path.join(ROOT, "tuning", "universe.txt")).read().split()
    years = float(cfg.get("learn_years", 2))
    record = {"run_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "strategy_version": version,
              "current": rsch.describe(cur, cur_edge), "changed": False}

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
        fetch(symbols + ["SPY"], years, log)
    data, src = rsch.load(set(symbols))
    dates = sorted({d for S in data.values() for d, _ in S})
    if len(data) < 15 or len(dates) < 45:
        log("Zu wenig Daten (%d Aktien, %d Tage): nichts geändert" % (len(data), len(dates)))
        record.update(result="zu wenig Daten", log=lines)
        return finish(record, a)
    big = src == "alpaca"
    split = dates[len(dates) * 2 // 3]                                  # stage 1: older 2/3 vs newest 1/3
    recent = dates[max(len(dates) // 3, len(dates) - 160)]             # stage 2: walk-forward, newest days
    record["data"] = {"source": src, "symbols": len(data), "days": len(dates), "from": dates[0], "to": dates[-1],
                      "spy": "SPY" in data}
    log("Daten: %s, %d Aktien, %d Handelstage (%s bis %s)%s" % (
        "Alpaca (Jahre)" if big else "Yahoo (60 Tage)", len(data), len(dates), dates[0], dates[-1],
        "" if "SPY" in data else ", SPY fehlt: Marktfilter ohne Wirkung"))

    rowsets, results = rsch.screen(data, split, 150 if big else 60, 60 if big else 25)
    short = [r["settings"] for r in rsch.stage1_pick(results, STAGE2_KEEP)]
    ck = json.dumps(cur, sort_keys=True)
    if ck not in {json.dumps(x, sort_keys=True) for x in short}:
        short.append(dict(cur))
    log("Stufe 1: %d Kombinationen geprüft, %d in die engere Wahl" % (len(results), len(short)))

    results2 = []
    for st in short:
        key_rs = (int(st["or_minutes"]), st["last_entry"])
        if key_rs not in rowsets:
            rowsets[key_rs] = rsch.build_rows(data, *key_rs)
        res = rsch.gated(data, rowsets[key_rs], st, recent, EDGES, 400 if big else REPLAYS)
        for e in EDGES:
            sm = summary(res[e])
            if sm["n"]:
                results2.append(dict(sm, settings=st, edge=e, label=rsch.describe(st, e),
                                     current=json.dumps(st, sort_keys=True) == ck and e == cur_edge))
    results = results2
    min_trades = MIN_TRADES * 3 if big else MIN_TRADES_SHORT_DATA
    if not big:
        # 60 days are one market phase: a falling month makes "short only" look brilliant. The
        # direction (long / short / both) may only change on years of data.
        dropped = [r for r in results if r["settings"].get("sides", "long") != cur.get("sides", "long")]
        results = [r for r in results if r["settings"].get("sides", "long") == cur.get("sides", "long")]
        if dropped:
            log("Richtungswechsel (%s) nur mit Jahresdaten: %d Kandidaten mit anderer Richtung nicht zugelassen"
                % (cur.get("sides", "long"), len(dropped)))
    results.sort(key=lambda r: -(r["lcb"] if r["n"] >= min_trades else -9))
    cur_r = next((r for r in results if r["current"]), {"n": 0, "mean": None, "lcb": None})
    best = next((r for r in results if r["n"] >= min_trades), None)
    record["top"] = [{k: r[k] for k in ("label", "n", "mean", "lcb", "win_pct", "current")} for r in results[:8]]
    record["current_eval"] = {k: cur_r.get(k) for k in ("n", "mean", "lcb", "win_pct")}
    log("Aktueller Plan (walk-forward): n=%s, Ø %s R" % (cur_r.get("n"), cur_r.get("mean")))
    for r in results[:3]:
        log("  Kandidat: %s → n=%d, Ø %+.3f R (vorsichtig %+.3f)" % (r["label"], r["n"], r["mean"], r["lcb"]))
    MIN_N = min_trades

    switch = bool(best and best["mean"] and best["mean"] > 0 and not best["current"] and
                  (cur_r.get("lcb") is None or cur_r["n"] < MIN_N or best["lcb"] >= cur_r["lcb"] + MIN_GAIN_R))
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
    # setup B: the 5-minute range on stocks in play, judged on its own with the current exits
    try:
        b_cfg = (rj(ap_path) or cfg).get("setup_b") or {"enabled": True, "or_minutes": 5, "last_entry": "10:30"}
        b_rows = rsch.in_play_rows(rsch.build_rows(data, int(b_cfg.get("or_minutes", 5)), b_cfg.get("last_entry", "10:30")))
        b_set = dict(cur if not switch else best["settings"], or_minutes=int(b_cfg.get("or_minutes", 5)),
                     last_entry=b_cfg.get("last_entry", "10:30"), max_pct_over=0, gap_filter=False, rvol_min=0)
        b_edge = cur_edge if not switch else best["edge"]
        b_res = summary(rsch.gated(data, b_rows, b_set, recent, [b_edge], 400 if big else REPLAYS)[b_edge])
        record["setup_b"] = dict(b_res, enabled_before=bool(b_cfg.get("enabled", True)))
        log("Setup B (5-Min-Range, Stocks in Play): n=%s, Ø %s R" % (b_res["n"], b_res["mean"]))
        want = None
        if b_res["n"] >= min_trades and b_res["mean"] is not None:
            if b_cfg.get("enabled", True) and b_res["mean"] < 0:
                want = False
            elif not b_cfg.get("enabled", True) and b_res["lcb"] > 0:
                want = True
        if want is not None:
            record["setup_b"]["enabled_after"] = want
            log("Setup B wird %s" % ("eingeschaltet" if want else "ausgeschaltet"))
            if not a.dry_run:
                new = rj(ap_path) or dict(cfg)
                new["setup_b"] = dict(b_cfg, enabled=want)
                wj(ap_path, new)
    except Exception as e:
        log("Setup B nicht bewertet: %s" % str(e)[:120])
    record["seconds"] = round(time.time() - t0)
    record["log"] = lines
    return finish(record, a)


def finish(record, a):
    if not a.dry_run:
        try:
            import notify
            notify.send("🧠 Wöchentliches Lernen: %s" % record.get("result", ""))
        except Exception:
            pass
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
