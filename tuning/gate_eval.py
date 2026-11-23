"""Walk-forward test of the Time Machine as a trade filter.

For every breakout the rule would have taken, the Time Machine is run exactly as the
autopilot runs it, but only on the sessions BEFORE that day (no look-ahead): the
Monte Carlo (2,000 replays here, for speed) and the history replay of the same rule.
Then several gate settings are compared on the same trades.

    python3 tuning/gate_eval.py
"""
import itertools, json, os, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402

tm = bt.tm
FLAT = bt.FLAT_SLOT
CONFIGS = {
    "A pack default: OR15 mid 1R": dict(or_minutes=15, stop="or_mid", target_r=1.0, last_entry_slot=35, max_pct_over=0, min_volume_x=0, gap_filter=False),
    "B OR15 mid 1.5R <=11:25 over<=.75": dict(or_minutes=15, stop="or_mid", target_r=1.5, last_entry_slot=23, max_pct_over=0.75, min_volume_x=0, gap_filter=False),
    "C OR15 mid 2R <=11:25 over<=.75": dict(or_minutes=15, stop="or_mid", target_r=2.0, last_entry_slot=23, max_pct_over=0.75, min_volume_x=0, gap_filter=False),
    "D OR30 mid 2R <=12:25 volx>=1": dict(or_minutes=30, stop="or_mid", target_r=2.0, last_entry_slot=35, max_pct_over=0, min_volume_x=1.0, gap_filter=False),
    "E OR15 mid 2R <=11:25 over<=.75 gap": dict(or_minutes=15, stop="or_mid", target_r=2.0, last_entry_slot=23, max_pct_over=0.75, min_volume_x=0, gap_filter=True),
}
GATES = [dict(edge=e, hist=h, avgR=a) for e, h, a in itertools.product([None, 0, 2, 5], [None, -0.05, 0.0], [None, 0.0])]


def signals(S, p):
    out = []
    prev = None
    for idx, (d, D) in enumerate(S):
        n = p["or_minutes"] // 5
        oh, ol = tm.opening_range(D, n)
        avg_v = sum(D[i]["v"] for i in range(n)) / n or 1
        k = next((i for i in range(n, p["last_entry_slot"] + 1) if D[i]["c"] > oh), None)
        pc, prev = prev, D[-1]["c"]
        if k is None:
            continue
        sig = round(D[k]["c"], 2)
        if p["max_pct_over"] and (sig - oh) / oh * 100 > p["max_pct_over"]:
            continue
        if p["min_volume_x"] and D[k]["v"] < p["min_volume_x"] * avg_v:
            continue
        if p["gap_filter"] and pc and D[0]["o"] < pc:
            continue
        stop = tm.cents_away((oh + ol) / 2 if p["stop"] == "or_mid" else ol, True)
        risk = sig - stop
        if risk <= 0 or not (0.15 <= risk / sig * 100 <= 2.5):
            continue
        target = round(sig + p["target_r"] * risk, 2)
        fill = D[k + 1]["o"]
        if fill <= stop or fill >= target:
            continue
        ex = None
        for b in D[k + 1:FLAT + 1]:
            if b["l"] <= stop:
                ex = stop
                break
            if b["h"] >= target:
                ex = target
                break
        ex = D[FLAT]["c"] if ex is None else ex
        out.append(dict(idx=idx, date=d, k=k, entry=sig, stop=stop, target=target, R=(ex - fill) / risk))
    return out


def main():
    import glob
    data = {}
    for f in sorted(glob.glob(os.path.join(bt.HERE, "data", "*-60d5m.json.gz"))):
        s = bt.sessions_of(f)
        if len(s) >= 40:
            data[os.path.basename(f).split("-")[0]] = s
    report = {}
    for name, p in CONFIGS.items():
        rule = {"setup": "orb", "or_minutes": p["or_minutes"], "stop_rule": p["stop"], "target_r": p["target_r"]}
        rows = []
        for sym, S in data.items():
            for s in signals(S, p):
                past = S[max(0, s["idx"] - 59):s["idx"]]
                if len(past) < 20:
                    continue
                pool = tm.build_pool(past)
                mc = tm.simulate(pool, s["entry"], s["stop"], s["target"], s["k"], FLAT, replays=2000, seed=19)
                h = tm.history_replay(past, rule, FLAT)
                sided = (mc["p_target"] + mc["p_stop"]) or 1e-9
                edge = (mc["p_target"] / sided - 1 / (1 + p["target_r"])) * 100
                rows.append(dict(R=s["R"], edge=edge, mcR=mc["avg_R"], hR=h["avg_R"], hn=h["triggered"]))
        base = [r["R"] for r in rows]
        res = []
        for g in GATES:
            sel = [r["R"] for r in rows if (g["edge"] is None or r["edge"] >= g["edge"])
                   and (g["avgR"] is None or r["mcR"] >= g["avgR"])
                   and (g["hist"] is None or r["hn"] < 8 or (r["hR"] or 0) >= g["hist"])]
            if len(sel) >= 25:
                res.append((statistics.mean(sel), len(sel), g))
        res.sort(key=lambda x: -x[0])
        report[name] = {"n": len(base), "avg_R": statistics.mean(base) if base else None,
                        "best": [{"avg_R": round(a, 4), "n": n, "gate": g} for a, n, g in res[:6]]}
        print("\n%s: %d trades, ungated avg R %+.3f" % (name, len(base), statistics.mean(base) if base else 0))
        for a, n, g in res[:6]:
            print("   gate edge>=%-4s hist>=%-5s mcR>=%-4s -> n=%-4d avg R %+.3f" % (g["edge"], g["hist"], g["avgR"], n, a))
    json.dump(report, open(os.path.join(bt.HERE, "gate_results.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
