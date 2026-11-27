"""Compare every tactic in autopilot/tactics.py on the same history.

    python3 tuning/compare.py            # Alpaca years if downloaded, else Yahoo's 60 days

Splits the days in two halves: a tactic that only works in one half is luck, not an edge.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "autopilot"))
import research  # noqa: E402
import tactics  # noqa: E402


def compare(data):
    dates = sorted({d for S in data.values() for d, _ in S})
    half = dates[len(dates) // 2]
    res = tactics.run(data)
    rows = []
    for name, trades in res.items():
        a = tactics.summary([t for t in trades if t["date"] < half])
        b = tactics.summary([t for t in trades if t["date"] >= half])
        rows.append(dict(tactics.summary(trades), name=name, first=a, second=b))
    rows.sort(key=lambda r: -(r.get("sum_R") or -999))
    return rows, dates


if __name__ == "__main__":
    data, src = research.load()
    rows, dates = compare(data)
    print("%s: %d Aktien, %d Tage (%s bis %s), $20 pro R ($2.000-Konto, 1 %% Risiko)\n" % (
        src, len(data) - 1, len(dates), dates[0], dates[-1]))
    print("%-20s %5s %6s %8s %8s %9s %6s %7s | %-18s %-18s" % ("Taktik", "Trades", "Treff", "Ø R", "Σ R", "Σ $",
                                                              "PF", "MaxDD", "1. Hälfte Ø R", "2. Hälfte Ø R"))
    for r in rows:
        if not r.get("n"):
            print("%-20s keine Trades" % r["name"]); continue
        print("%-20s %5d %5.1f%% %+8.3f %+8.1f %+9.0f %6s %6.1fR | n=%-4s %+.3f      n=%-4s %+.3f" % (
            r["name"], r["n"], r["win_pct"], r["avg_R"], r["sum_R"], r["usd"], r["profit_factor"], r["max_dd_R"],
            r["first"].get("n"), r["first"].get("avg_R") or 0, r["second"].get("n"), r["second"].get("avg_R") or 0))
