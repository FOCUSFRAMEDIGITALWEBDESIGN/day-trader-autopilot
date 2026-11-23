"""Strategy research on long histories: opening-range breakouts with filters and exits.

Every first breakout of a session is turned into ONE row with its features (how far
over the range, the gap, relative volume, the market, relative strength) and its
result in R for every stop / target / exit combination. A strategy is then just a
filter over the rows, so thousands of combinations are cheap to compare.

The fill is the open of the bar after the signal (a market order sent when the
signal bar closed), every trade is flat at the 15:50 bar's close (flat_by 15:55).
Filters and exits come from autopilot/features.py, the same code the live bot runs.

    python3 tuning/research.py            # prints the best settings, in- and out-of-sample
"""
import glob, itertools, json, os, statistics, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import backtest as bt  # noqa: E402
import features as fx  # noqa: E402

tm = bt.tm
FLAT = bt.FLAT_SLOT
STOPS = ["or-mid", "or-low"]
TARGETS = ["1.5R", "2R", "3R"]
EXITS = ["fixed", "breakeven", "partial", "trail_r", "trail_atr:1.5", "trail_atr:3"]
ROWSETS = {"or_minutes": [15, 30], "last_entry": ["10:30", "11:30", "12:30"]}
FILTERS = {"max_pct_over": [0.75, 0], "gap_filter": [True, False], "rvol_min": [0, 1.5, 2.5],
           "market_filter": ["none", "green"], "rs_min": [None, 0.0]}


def hm(t):
    h, m = str(t).split(":")[:2]
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- data
def load_dir(pattern):
    out = {}
    for f in sorted(glob.glob(pattern)):
        sym = os.path.basename(f).split("-")[0]
        try:
            S = bt.sessions_of(f)
        except Exception:
            continue
        if len(S) >= 40:
            out[sym] = S
    return out


def load(symbols=None):
    """Alpaca years if there are enough of them, else Yahoo's 60 days. (data, source)"""
    al = load_dir(os.path.join(HERE, "data_alpaca", "*-alpaca5m.json.gz"))
    if symbols:
        al = {k: v for k, v in al.items() if k in symbols or k == "SPY"}
    if len(al) >= 15:
        return al, "alpaca"
    yh = load_dir(os.path.join(HERE, "data", "*-60d5m.json.gz"))
    if symbols:
        yh = {k: v for k, v in yh.items() if k in symbols or k == "SPY"}
    return yh, "yahoo"


# ---------------------------------------------------------------- rows
def build_rows(data, or_minutes, last_entry):
    n = or_minutes // 5
    last_slot = (hm(last_entry) - 570) // 5 - 1
    spy = {d: D for d, D in data.get("SPY", [])}
    rows = []
    for sym, S in data.items():
        if sym == "SPY":
            continue
        for idx in range(1, len(S)):
            d, D = S[idx]
            oh, ol = tm.opening_range(D, n)
            k = next((i for i in range(n, last_slot + 1) if D[i]["c"] > oh), None)
            if k is None or k + 1 > FLAT:
                continue
            sig = round(D[k]["c"], 2)
            fill = D[k + 1]["o"]
            prev_c = S[idx - 1][1][-1]["c"]
            past = [P for _, P in S[max(0, idx - 14):idx]]
            sp = spy.get(d)
            row = {"sym": sym, "date": d, "idx": idx, "k": k, "entry": sig, "fill": fill,
                   "pct_over": (sig - oh) / oh * 100, "gap": (D[0]["o"] - prev_c) / prev_c * 100,
                   "rvol": fx.rvol(D, past, n), "market_ok": fx.market_ok(sp, k, "green"),
                   "rs": fx.rel_strength(D, sp, k), "R": {}, "plan": {}}
            after = D[k + 1:FLAT + 1]
            a14 = fx.atr(D[:k + 1])
            for st in STOPS:
                stop = tm.cents_away((oh + ol) / 2 if st == "or-mid" else ol, True)
                risk = sig - stop
                if risk <= 0 or not (0.15 <= risk / sig * 100 <= 2.5):
                    continue
                for tg in TARGETS:
                    target = round(sig + float(tg[:-1]) * risk, 2)
                    if fill <= stop or fill >= target:
                        continue
                    row["plan"][(st, tg)] = (stop, target)
                    for ex in EXITS:
                        mode, _, kk = ex.partition(":")
                        done = [row["R"][(st, t0, ex)] for t0 in TARGETS if (st, t0, ex) in row["R"]]
                        if mode in fx.TRAILING and done:
                            # trailing exits have no target: the same result for every target
                            row["R"][(st, tg, ex)] = done[0]
                            continue
                        row["R"][(st, tg, ex)] = fx.simulate_exit(after, fill, stop, target, risk, mode,
                                                                  atr_value=a14, k=float(kk or 2))
            if row["R"]:
                rows.append(row)
    return rows


def keep(row, f):
    if f.get("max_pct_over") and row["pct_over"] > f["max_pct_over"]:
        return False
    if f.get("gap_filter") and row["gap"] < 0:
        return False
    ok, _ = fx.passes({"rvol": row["rvol"], "market_ok": row["market_ok"], "rs": row["rs"]}, f)
    return ok


def summary(rs):
    if not rs:
        return {"n": 0, "mean": None, "lcb": None, "win_pct": None}
    m = statistics.mean(rs)
    se = statistics.stdev(rs) / len(rs) ** 0.5 if len(rs) > 1 else 1.0
    return {"n": len(rs), "mean": round(m, 4), "lcb": round(m - se, 4),
            "win_pct": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1)}


def settings(rs_key, f, out_key):
    s = dict(rs_key)
    s.update(f)
    mode, _, kk = out_key[2].partition(":")
    s.update({"stop": out_key[0], "target": out_key[1], "exit_mode": mode, "min_volume_x": None})
    if kk:
        s["exit_atr_k"] = float(kk)
    return s


def screen(data, split, min_in=60, min_out=25):
    """Stage 1: every combination, plain rule (no Time Machine). Returns (rowsets, results)."""
    rowsets, results = {}, []
    for o, le in itertools.product(ROWSETS["or_minutes"], ROWSETS["last_entry"]):
        if hm(le) < 570 + o + 15:
            continue
        rows = build_rows(data, o, le)
        rowsets[(o, le)] = rows
        for combo in itertools.product(*FILTERS.values()):
            f = dict(zip(FILTERS, combo))
            sel = [r for r in rows if keep(r, f)]
            for out_key in itertools.product(STOPS, TARGETS, EXITS):
                ins = [r["R"][out_key] for r in sel if r["date"] < split and out_key in r["R"]]
                oos = [r["R"][out_key] for r in sel if r["date"] >= split and out_key in r["R"]]
                if len(ins) < min_in or len(oos) < min_out:
                    continue
                si, so = summary(ins), summary(oos)
                results.append({"settings": settings({"or_minutes": o, "last_entry": le}, f, out_key),
                                "in": si, "out": so})
    return rowsets, results


def stage1_pick(results, keep_n=6):
    """Best by in-sample lower bound, among those that are also positive out-of-sample."""
    good = [r for r in results if r["out"]["mean"] is not None and r["out"]["mean"] > 0]
    good.sort(key=lambda r: -r["in"]["lcb"])
    return good[:keep_n]


_mc_cache = {}


def gated(data, rows, s, recent_from, edges=(0.0, 2.0, 5.0), replays=600):
    """Stage 2: the Time Machine gate, walk-forward (only sessions before each day)."""
    ex = s["exit_mode"] + (":%g" % float(s.get("exit_atr_k", 2)) if s["exit_mode"] == "trail_atr" else "")
    out_key = (s["stop"], s["target"], ex)
    tr = float(s["target"][:-1])
    res = {e: [] for e in edges}
    S_by = data
    for r in rows:
        if r["date"] < recent_from or out_key not in r["R"] or not keep(r, s):
            continue
        S = S_by[r["sym"]]
        past = S[max(0, r["idx"] - 59):r["idx"]]
        if len(past) < 20:
            continue
        stop, target = r["plan"][(s["stop"], s["target"])]
        ck = (r["sym"], r["date"], r["k"], stop, target)
        mc = _mc_cache.get(ck)
        if mc is None:
            m = tm.simulate(tm.build_pool(past), r["entry"], stop, target, r["k"], FLAT, replays=replays, seed=19)
            mc = _mc_cache[ck] = (m["p_target"], m["p_stop"], m["avg_R"])
        sided = (mc[0] + mc[1]) or 1e-9
        edge = (mc[0] / sided - 1 / (1 + tr)) * 100
        if mc[2] < 0:
            continue
        for e in edges:
            if edge >= e:
                res[e].append(r["R"][out_key])
    return res


def exit_label(s):
    m = s.get("exit_mode", "fixed")
    return {"fixed": "fest", "breakeven": "Stop auf Einstieg ab 1R", "partial": "halb bei 1R",
            "trail_r": "Trailing in R-Stufen, ohne Ziel",
            "trail_atr": "Trailing %g× ATR, ohne Ziel" % float(s.get("exit_atr_k", 2))}.get(m, m)


def describe(s, edge=None):
    bits = ["OR%s" % s["or_minutes"], s["stop"], "TM-Ziel " + s["target"] if s.get("exit_mode") in fx.TRAILING
            else s["target"], "bis " + s["last_entry"],
            "Ausstieg " + exit_label(s)]
    if s.get("max_pct_over"):
        bits.append("≤%s%% über Range" % s["max_pct_over"])
    if s.get("gap_filter"):
        bits.append("Gap ≥ 0")
    if s.get("rvol_min"):
        bits.append("RVOL ≥ %s" % s["rvol_min"])
    if s.get("market_filter") == "green":
        bits.append("SPY grün")
    if s.get("rs_min") is not None:
        bits.append("stärker als SPY")
    if edge is not None:
        bits.append("TM-Edge ≥ %g" % edge)
    return ", ".join(bits)


if __name__ == "__main__":
    data, src = load()
    dates = sorted({d for S in data.values() for d, _ in S})
    split = dates[len(dates) * 2 // 3]
    big = src == "alpaca"
    print("%s: %d Aktien, %d Tage (%s bis %s), SPY %s" % (src, len(data), len(dates), dates[0], dates[-1],
                                                         "ja" if "SPY" in data else "FEHLT"))
    t0 = datetime.datetime.now()
    rowsets, results = screen(data, split, 150 if big else 60, 60 if big else 25)
    print("%d Kombinationen in %ds" % (len(results), (datetime.datetime.now() - t0).seconds))
    for r in stage1_pick(results, 15):
        print("IN n=%-5d Ø%+.3f | OUT n=%-5d Ø%+.3f | %s" % (r["in"]["n"], r["in"]["mean"], r["out"]["n"],
                                                           r["out"]["mean"], describe(r["settings"])))
