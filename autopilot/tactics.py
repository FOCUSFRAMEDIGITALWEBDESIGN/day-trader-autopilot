"""Several day-trading tactics, run side by side on the same candles, so they can be compared fairly.

Every tactic is a fixed rule (decided before the day starts, no look-ahead): when to enter, where the
stop and the target go, and how it exits. Each produces trades in R (1 R = the planned risk).
The fill is the open of the bar after the signal, everything is flat at the 15:50 bar's close.

  ORB 15 2R          the live plan: 15-minute opening-range breakout, stop at the range middle, 2R
  ORB 15 Trailing    the same entries, trailing stop at 1.5 x ATR instead of a fixed target
  ORB 15 Schlusskurs the same, but out only when a 5-minute bar CLOSES beyond the stop (hard stop at 1.5R)
  ORB 15 + Stärke    the live plan, only when the stock is stronger than SPY and SPY agrees
  ORB 30 2R          30-minute range, signals until 12:30
  ORB 5 In-Play      5-minute range, only stocks in play (gap >= 1 %, relative volume >= 1.5), until 10:30
  VWAP-Rücksetzer    a trending stock dips to its VWAP and closes back on the right side: with the trend
  Gap-Fade           a gap of 2 % or more that fails by 10:00 is traded back toward yesterday's close

Long and short alike (a short is the long rule on the mirrored price, features.mirror).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tuning"))
import features as fx  # noqa: E402

FLAT = 76          # the 15:50 bar closes at 15:55
MIN_RISK, MAX_RISK = 0.15, 2.5   # risk as % of the price


def _cents_down(x):
    """Round a stop to the cent AWAY from the trade (down in the oriented series, shorts too)."""
    import math
    return math.floor(round(x * 100, 4)) / 100.0


def vwap_series(D, upto):
    """VWAP after each bar 0..upto (typical price x volume)."""
    out, pv, vv = [], 0.0, 0.0
    for i in range(upto + 1):
        b = D[i]
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * (b["v"] or 0)
        vv += (b["v"] or 0)
        out.append(pv / vv if vv else b["c"])
    return out


def _trade(sym, date, side, D, k, stop, target, exit_mode="fixed", atr=None, k_atr=1.5, tactic=""):
    """Enter at the open of bar k+1 in the oriented series D (long space). -> trade dict or None."""
    if k + 1 > FLAT:
        return None
    sig = round(D[k]["c"], 2)
    fill = D[k + 1]["o"]
    risk = sig - stop
    if risk <= 0 or not (MIN_RISK <= risk / abs(sig) * 100 <= MAX_RISK):
        return None
    if fill <= stop or (target is not None and fill >= target):
        return None
    after = D[k + 1:FLAT + 1]
    if exit_mode == "close_stop":
        r = None
        hard = sig - 1.5 * risk
        for i, b in enumerate(after):
            if b["l"] <= hard:
                r = (hard - fill) / risk
                break
            if b["h"] >= target:
                r = (target - fill) / risk
                break
            if b["c"] < stop:
                nxt = after[i + 1]["o"] if i + 1 < len(after) else b["c"]
                r = (nxt - fill) / risk
                break
        if r is None:
            r = (after[-1]["c"] - fill) / risk
    else:
        r = fx.simulate_exit(after, fill, stop, target if target is not None else fill + 999 * risk, risk,
                             exit_mode, atr_value=atr, k=k_atr)
    return {"tactic": tactic, "sym": sym, "date": date, "side": side, "bar": k, "R": round(r, 6)}


def ochg(D, k):
    """Move since the open in % in the ORIENTED series (positive = in the trade's favour, shorts too)."""
    try:
        return (D[k]["c"] - D[0]["o"]) / abs(D[0]["o"]) * 100
    except (IndexError, KeyError, ZeroDivisionError, TypeError):
        return None


# ---------------------------------------------------------------- the tactic families
def orb(sym, date, side, D, prev_c, past, spy, p):
    n = p["or_minutes"] // 5
    last = p["last_slot"]
    oh = max(D[i]["h"] for i in range(n))
    ol = min(D[i]["l"] for i in range(n))
    k = next((i for i in range(n, min(last, FLAT - 1) + 1) if D[i]["c"] > oh), None)
    if k is None:
        return None
    if p.get("max_over") and (D[k]["c"] - oh) / abs(oh) * 100 > p["max_over"]:
        return None
    gap = (D[0]["o"] - prev_c) / abs(prev_c) * 100
    if p.get("gap_filter") and gap < 0:
        return None
    if p.get("in_play"):
        rv = fx.rvol(D, past, n)
        if abs(gap) < 1.0 or (rv or 0) < 1.5:
            return None
    if p.get("strength"):
        ch = ochg(spy, k) if spy else None
        if ch is not None and ch < 0:
            return None                      # the market is against the trade
        a = ochg(D, k)
        if a is not None and ch is not None and a - ch < 0:
            return None                      # the stock is not stronger than the market
    stop = _cents_down((oh + ol) / 2)
    risk = round(D[k]["c"], 2) - stop
    target = round(round(D[k]["c"], 2) + p.get("target_r", 2.0) * risk, 2)
    atr = fx.atr(D[:k + 1]) if p.get("exit") == "trail_atr" else None
    return _trade(sym, date, side, D, k, stop, target, p.get("exit", "fixed"), atr, 1.5, p["name"])


def vwap_pullback(sym, date, side, D, prev_c, past, spy, p):
    """From 10:00 to 12:30: the last 6 closes above VWAP and the day green; then a bar dips to or
    below VWAP and CLOSES back above it. Stop below that bar's low, target 2R."""
    vw = vwap_series(D, min(FLAT, len(D) - 1))
    for k in range(6, min(35, FLAT - 1) + 1):
        if not all(D[i]["c"] > vw[i] for i in range(k - 6, k)):
            continue
        if D[k - 1]["c"] <= D[0]["o"]:
            continue
        if D[k]["l"] <= vw[k] < D[k]["c"]:
            stop = _cents_down(min(D[k]["l"], vw[k]) - 0.0005 * abs(D[k]["c"]))
            risk = D[k]["c"] - stop
            return _trade(sym, date, side, D, k, stop, D[k]["c"] + 2 * risk, "fixed", tactic=p["name"])
    return None


def gap_fade(sym, date, side, D, prev_c, past, spy, p):
    """Oriented: the stock opened 2 %+ AGAINST the trade direction (a gap down for a long, a gap up for a
    short). If by 10:00 it closes back above its VWAP and the 30-minute range middle, trade the gap back
    toward yesterday's close. Stop under the 30-minute low; the target is the gap fill, at least 1R."""
    gap = (D[0]["o"] - prev_c) / abs(prev_c) * 100
    if gap > -2.0:
        return None
    k = 5                                   # the 9:55 bar closes at 10:00
    vw = vwap_series(D, k)
    oh = max(D[i]["h"] for i in range(6))
    ol = min(D[i]["l"] for i in range(6))
    if not (D[k]["c"] > vw[k] and D[k]["c"] > (oh + ol) / 2):
        return None
    stop = _cents_down(ol)
    risk = D[k]["c"] - stop
    target = prev_c
    if risk <= 0 or (target - D[k]["c"]) < risk:
        return None
    return _trade(sym, date, side, D, k, stop, target, "fixed", tactic=p["name"])


TACTICS = [
    {"name": "ORB 15 2R", "fn": orb, "or_minutes": 15, "last_slot": 23, "max_over": 0.75, "gap_filter": True},
    {"name": "ORB 15 Trailing", "fn": orb, "or_minutes": 15, "last_slot": 23, "max_over": 0.75, "gap_filter": True,
     "exit": "trail_atr"},
    {"name": "ORB 15 Schlusskurs", "fn": orb, "or_minutes": 15, "last_slot": 23, "max_over": 0.75,
     "gap_filter": True, "exit": "close_stop"},
    {"name": "ORB 15 + Stärke", "fn": orb, "or_minutes": 15, "last_slot": 23, "max_over": 0.75, "gap_filter": True,
     "strength": True},
    {"name": "ORB 30 2R", "fn": orb, "or_minutes": 30, "last_slot": 35, "max_over": 0.75, "gap_filter": True},
    {"name": "ORB 5 In-Play", "fn": orb, "or_minutes": 5, "last_slot": 11, "in_play": True},
    {"name": "VWAP-Rücksetzer", "fn": vwap_pullback},
    {"name": "Gap-Fade", "fn": gap_fade},
]


def run(data, dates=None, tactics=TACTICS, sides=("buy", "sell")):
    """data = {SYM: [(date, [bars of a full session]), ...]} oldest first, with "SPY" if available.
    dates: only trade these dates (the sessions before them still serve as history).
    -> {tactic name: [trade, ...]}"""
    spy = {d: D for d, D in data.get("SPY", [])}
    out = {t["name"]: [] for t in tactics}
    want = set(dates) if dates else None
    for sym, S in data.items():
        if sym == "SPY":
            continue
        for idx in range(1, len(S)):
            date, Dr = S[idx]
            if want and date not in want:
                continue
            if len(Dr) <= FLAT:
                continue
            prev_c = S[idx - 1][1][-1]["c"]
            past_r = [P for _, P in S[max(0, idx - 14):idx]]
            for side in sides:
                if side == "buy":
                    D, pc, past, sp = Dr, prev_c, past_r, spy.get(date)
                else:
                    D, pc, past = fx.mirror(Dr), -prev_c, past_r
                    sp = fx.mirror(spy[date]) if date in spy else None
                for t in tactics:
                    try:
                        tr = t["fn"](sym, date, side, D, pc, past, sp, t)
                    except (IndexError, ZeroDivisionError, KeyError):
                        tr = None
                    if tr:
                        out[t["name"]].append(tr)
    return out


def summary(trades, dollars_per_r=20.0):
    if not trades:
        return {"n": 0}
    rs = [t["R"] for t in trades]
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["date"], x.get("bar", 0))):
        eq += t["R"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wins = sum(1 for r in rs if r > 0)
    gross_w = sum(r for r in rs if r > 0)
    gross_l = -sum(r for r in rs if r < 0)
    return {"n": len(rs), "win_pct": round(100 * wins / len(rs), 1), "avg_R": round(sum(rs) / len(rs), 3),
            "sum_R": round(sum(rs), 2), "usd": round(sum(rs) * dollars_per_r, 2),
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None, "max_dd_R": round(dd, 2),
            "long": sum(1 for t in trades if t["side"] == "buy"), "short": sum(1 for t in trades if t["side"] == "sell")}


def leaderboard(results, dollars_per_r=20.0):
    rows = [dict(summary(v, dollars_per_r), name=k) for k, v in results.items()]
    rows.sort(key=lambda r: -(r.get("sum_R") or -9999))
    return rows


# ---------------------------------------------------------------- live data for the daily comparison
def _ny(ts):
    """UTC epoch -> (New York date 'YYYY-MM-DD', minutes after midnight), DST like the pack."""
    import datetime
    utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)
    y = utc.year

    def nth_sunday(month, n):
        d = datetime.datetime(y, month, 1)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    dst = nth_sunday(3, 2) + datetime.timedelta(hours=7) <= utc < nth_sunday(11, 1) + datetime.timedelta(hours=6)
    local = utc - datetime.timedelta(hours=4 if dst else 5)
    return local.date().isoformat(), local.hour * 60 + local.minute


def sessions_from_alpaca(bars):
    """Alpaca 5-minute bars of one symbol -> [(date, [78 Bars])] for every COMPLETE regular session."""
    import calendar
    import time as _t
    days = {}
    for b in bars:
        ts = calendar.timegm(_t.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S"))
        date, m = _ny(ts)
        if 570 <= m < 960 and ts % 300 == 0:
            days.setdefault(date, {})[(m - 570) // 5] = fx.Bar(b["o"], b["h"], b["l"], b["c"], b["v"])
    out = []
    for d in sorted(days):
        D = days[d]
        if all(i in D for i in range(78)):
            out.append((d, [D[i] for i in range(78)]))
    return out
