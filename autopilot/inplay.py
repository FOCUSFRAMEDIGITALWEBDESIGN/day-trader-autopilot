"""Stocks in play: the liquid stocks with a real reason to move today.

1. Once a week, the liquid universe: from the pack's stock list (trader/universe.txt,
   ~5,900 names) keep those with a price of at least `in_play_min_price` and the most dollar
   volume over the last 20 sessions (Alpaca daily bars, 100 symbols a request).
2. Each morning when the opening range is complete: Alpaca snapshots (200 symbols a request)
   give today's open, volume so far and yesterday's bar. A stock is in play when it gapped at
   least `in_play_min_gap` percent (up OR down) and trades at least `in_play_min_rvol` times
   its usual volume for this time of day. The strongest `in_play_max` join the day's scan list.
"""
import datetime
import json
import os
import time


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def build_liquid(api, universe, path, keep=1500, min_price=10.0, log=print, feed="sip"):
    end = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=35)).strftime("%Y-%m-%dT00:00:00Z")
    rows = []
    for batch in _chunks(universe, 100):
        try:
            got = api.multi_bars(batch, "1Day", start, end, feed=feed)
        except Exception as e:
            if feed == "sip":
                feed = "iex"
                got = api.multi_bars(batch, "1Day", start, end, feed=feed)
            else:
                log("liquid universe: a batch failed: %s" % str(e)[:100])
                continue
        for sym, bars in got.items():
            bars = bars[-20:]
            if len(bars) < 10 or bars[-1]["c"] < min_price:
                continue
            dv = sum(b["c"] * b["v"] for b in bars) / len(bars)
            avg_v = sum(b["v"] for b in bars) / len(bars)
            rows.append({"symbol": sym, "dollar_volume": round(dv), "avg_volume": round(avg_v), "price": bars[-1]["c"]})
        time.sleep(0.35)
    rows.sort(key=lambda r: -r["dollar_volume"])
    doc = {"built": datetime.date.today().isoformat(), "feed": feed, "symbols": rows[:keep]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    log("liquid universe: %d stocks kept of %d checked (feed %s)" % (len(doc["symbols"]), len(universe), feed))
    return doc


def rank(api, liquid, minutes_open, min_gap=1.0, min_rvol=1.5, max_n=30, feed="iex"):
    """-> the in-play list, strongest first: {symbol, gap, rvol, price}"""
    syms = [r["symbol"] for r in liquid]
    frac = max(minutes_open, 5) / 390.0
    out = []
    for batch in _chunks(syms, 200):
        snaps = api.snapshots(batch, feed=feed) or {}
        for sym, sn in snaps.items():
            db, pb = (sn or {}).get("dailyBar") or {}, (sn or {}).get("prevDailyBar") or {}
            if not db.get("o") or not pb.get("c") or not pb.get("v"):
                continue
            gap = (db["o"] / pb["c"] - 1) * 100
            rvol = db.get("v", 0) / (pb["v"] * frac)
            if abs(gap) >= min_gap and rvol >= min_rvol:
                out.append({"symbol": sym, "gap": round(gap, 2), "rvol": round(rvol, 2),
                            "price": ((sn.get("latestTrade") or {}).get("p")) or db.get("c")})
        time.sleep(0.2)
    out.sort(key=lambda r: -(r["rvol"] * (1 + abs(r["gap"]) / 4)))
    return out[:max_n]
