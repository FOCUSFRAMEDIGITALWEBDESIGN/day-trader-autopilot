"""Grid backtest of the opening-range-breakout rule the autopilot trades.

It re-uses the pack's own Time Machine code (parse_bars, sessions, rounding), so the
rule is exactly the one trader-entry / trader-exit apply, with two changes that make
it closer to what the autopilot really gets:

  * the fill is the OPEN of the bar after the trigger bar (a market order sent just
    after the trigger bar closed), not the trigger close; stop and target stay where
    trader-exit put them (measured from the trigger close);
  * every trade is flat at the close of the 15:50 bar (flat_by 15:55).

Results are in R (1R = the risk a share at the plan). Sessions are split in two
halves: the first half picks parameters (in-sample), the second half checks them
(out-of-sample) so the choice is not just curve fitting.

    python3 tuning/backtest.py                 # grid -> tuning/results.json + table
"""
import glob, gzip, importlib.util, itertools, json, os, statistics, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tm = _load("tm", "trader-timemachine/timemachine.py")
FLAT_SLOT = 76  # the 15:50 bar closes at 15:55


def sessions_of(path):
    with gzip.open(path, "rt") as f:
        text = f.read()
    res = tm.chart_result(text, os.path.basename(path).split("-")[0])
    now = datetime.datetime.utcnow()
    _, bars, _ = tm.parse_bars(res, now)
    days = tm.split_sessions(bars)
    full, _ = tm.full_sessions(days, "9999-12-31")
    return full  # [(date, [78 bars])]


def trade(D, prev_close, p):
    n = p["or_minutes"] // 5
    oh, ol = tm.opening_range(D, n)
    avg_v = sum(D[i]["v"] for i in range(n)) / n or 1
    k = next((i for i in range(n, p["last_entry_slot"] + 1) if D[i]["c"] > oh), None)
    if k is None or k + 1 > FLAT_SLOT:
        return None
    sig = round(D[k]["c"], 2)
    pct_over = (sig - oh) / oh * 100
    if p["max_pct_over"] and pct_over > p["max_pct_over"]:
        return None
    if p["min_volume_x"] and D[k]["v"] < p["min_volume_x"] * avg_v:
        return None
    if p["gap_filter"] and prev_close and D[0]["o"] < prev_close:
        return None  # only days that opened at or above yesterday's close
    stop = tm.cents_away((oh + ol) / 2 if p["stop"] == "or_mid" else ol, True)
    risk = sig - stop
    if risk <= 0 or risk / sig * 100 < p["min_risk_pct"] or risk / sig * 100 > p["max_risk_pct"]:
        return None
    target = round(sig + p["target_r"] * risk, 2)
    fill = D[k + 1]["o"]
    if fill <= stop or fill >= target:
        return None  # gapped through a side before the order filled: the pack refuses such a plan
    out = None
    for b in D[k + 1:FLAT_SLOT + 1]:
        if b["l"] <= stop:
            out = stop
            break
        if b["h"] >= target:
            out = target
            break
    if out is None:
        out = D[FLAT_SLOT]["c"]
    return (out - fill) / risk


GRID = {
    "or_minutes": [15, 30],
    "stop": ["or_mid", "or_low"],
    "target_r": [1.0, 1.5, 2.0, 3.0],
    "last_entry_slot": [11, 23, 35],      # last trigger bar 10:25, 11:25, 12:25
    "max_pct_over": [0, 0.75],
    "min_volume_x": [0, 1.0],
    "gap_filter": [False, True],
    "min_risk_pct": [0.15],
    "max_risk_pct": [2.5],
}


def stats(rs):
    if not rs:
        return {"n": 0}
    win = [r for r in rs if r > 0]
    loss = [-r for r in rs if r < 0]
    return {"n": len(rs), "avg_R": round(statistics.mean(rs), 4), "sum_R": round(sum(rs), 2),
            "win_pct": round(100 * len(win) / len(rs), 1),
            "pf": round(sum(win) / sum(loss), 3) if loss else None}


def main():
    files = sorted(glob.glob(os.path.join(HERE, "data", "*-60d5m.json.gz")))
    data = {}
    for f in files:
        try:
            s = sessions_of(f)
        except Exception as e:
            print("skip", f, e, file=sys.stderr)
            continue
        if len(s) >= 30:
            data[os.path.basename(f).split("-")[0]] = s
    dates = sorted({d for s in data.values() for d, _ in s})
    split = dates[len(dates) // 2]
    print("%d symbols, %d sessions (%s .. %s), split at %s" % (len(data), len(dates), dates[0], dates[-1], split))
    keys = list(GRID)
    rows = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, combo))
        if p["or_minutes"] == 30 and p["last_entry_slot"] < 11:
            continue
        ins, oos = [], []
        for sym, S in data.items():
            prev = None
            for d, D in S:
                r = trade(D, prev, p)
                prev = D[-1]["c"]
                if r is None:
                    continue
                (ins if d < split else oos).append(r)
        rows.append({"params": p, "in": stats(ins), "out": stats(oos), "all": stats(ins + oos)})
    # rank by in-sample expectancy with a minimum sample, then show out-of-sample next to it
    ok = [r for r in rows if r["in"].get("n", 0) >= 60 and r["out"].get("n", 0) >= 40]
    ok.sort(key=lambda r: r["in"]["avg_R"], reverse=True)
    json.dump({"split": split, "symbols": sorted(data), "sessions": len(dates), "rows": rows},
              open(os.path.join(HERE, "results.json"), "w"), indent=1)
    base = next(r for r in rows if r["params"] == {"or_minutes": 15, "stop": "or_mid", "target_r": 1.0,
                                                    "last_entry_slot": 35, "max_pct_over": 0, "min_volume_x": 0,
                                                    "gap_filter": False, "min_risk_pct": 0.15, "max_risk_pct": 2.5})
    print("baseline (pack default, reel plan):", base["in"], base["out"])
    print("top 15 by in-sample avg R:")
    for r in ok[:15]:
        p = r["params"]
        print("OR%-2d %-6s %.1fR last<=%d over<=%s volx>=%s gap=%-5s | IN n=%-4d avgR=%+.3f win=%4.1f%% | OUT n=%-4d avgR=%+.3f win=%4.1f%% pf=%s"
              % (p["or_minutes"], p["stop"], p["target_r"], p["last_entry_slot"], p["max_pct_over"], p["min_volume_x"],
                 p["gap_filter"], r["in"]["n"], r["in"]["avg_R"], r["in"]["win_pct"], r["out"]["n"],
                 r["out"]["avg_R"], r["out"]["win_pct"], r["out"]["pf"]))


if __name__ == "__main__":
    main()
