"""Trade filters and exit rules shared by the backtest, the learner and the live autopilot.

A session is indexable by 5-minute slot (0 = the 9:30 bar): a list of 78 bars, or a
{slot: bar} dict for today's partial session. A bar is {"o","h","l","c","v"}.
Keeping one implementation means the backtest measures exactly what the bot trades.
"""


def _has(D, n):
    try:
        return all(D[i] is not None for i in range(n))
    except (KeyError, IndexError):
        return False


def rvol(today, past, n, lookback=14):
    """Relative volume of the opening range: today's volume in the first n bars over
    the average of the same bars in up to `lookback` earlier sessions. None if unknown."""
    if not _has(today, n):
        return None
    prev = [sum(P[i]["v"] for i in range(n)) for P in past[-lookback:] if _has(P, n)]
    prev = [v for v in prev if v > 0]
    if len(prev) < 5:
        return None
    return sum(today[i]["v"] for i in range(n)) / (sum(prev) / len(prev))


def day_change(D, k):
    """Percent from the 9:30 open to the close of bar k."""
    if not _has(D, k + 1) or not D[0]["o"]:
        return None
    return (D[k]["c"] / D[0]["o"] - 1) * 100


def market_ok(spy, k, mode):
    """'none': always. 'green': SPY's close at bar k above its 9:30 open. Unknown data passes."""
    if mode in (None, "none", False):
        return True
    ch = day_change(spy, k) if spy is not None else None
    if ch is None:
        return True
    return ch > 0


def rel_strength(D, spy, k):
    """Stock's move since the open minus SPY's, in percentage points, at bar k."""
    a = day_change(D, k)
    b = day_change(spy, k) if spy is not None else None
    if a is None or b is None:
        return None
    return a - b


def passes(f, p):
    """f = the features of one signal, p = the settings. Returns (ok, reason)."""
    if p.get("rvol_min") and (f.get("rvol") is None or f["rvol"] < float(p["rvol_min"])):
        return False, "relative volume %s under %.1fx" % ("unknown" if f.get("rvol") is None else "%.2fx" % f["rvol"],
                                                         float(p["rvol_min"]))
    if p.get("market_filter") not in (None, "none") and not f.get("market_ok", True):
        return False, "the market (SPY) is red since the open"
    if p.get("rs_min") is not None and f.get("rs") is not None and f["rs"] < float(p["rs_min"]):
        return False, "relative strength %+.2f pts vs SPY, needs %+.2f" % (f["rs"], float(p["rs_min"]))
    return True, ""


TRAILING = ("trail_r", "trail_atr")


def atr(bars, n=14):
    """Average true range of the last n bars (fewer if the session is younger)."""
    trs, pc = [], None
    for b in bars:
        trs.append(b["h"] - b["l"] if pc is None else max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
        pc = b["c"]
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def trail_level(mode, fill, stop, risk, high, atr_value=None, k=2.0):
    """Where a trailing stop sits once the highest high since the fill is `high`.
    trail_r:   +1R -> stop at the fill, +2R -> fill + 1R, +3R -> fill + 2R, ...
    trail_atr: the highest high minus k x ATR
    Never below `stop`, rounded down to the cent."""
    st = stop
    if mode == "trail_r" and risk > 0:
        n = int((high - fill) / risk + 1e-9)
        if n >= 1:
            st = max(st, fill + (n - 1) * risk)
    elif mode == "trail_atr" and atr_value:
        st = max(st, high - k * atr_value)
    return int(st * 100 + 1e-6) / 100.0


def simulate_exit(bars, fill, stop, target, risk, mode, at_R=1.0, atr_value=None, k=2.0):
    """Walk the bars after the fill. Returns R (per full position).

    fixed:     stop / target / last close
    breakeven: once a bar's high reaches fill + at_R x risk, the stop moves to the fill
               (from the NEXT bar on; inside one bar the stop is checked first)
    partial:   at fill + at_R x risk half is sold there and the stop moves to the fill;
               the other half runs to the target, the new stop or the last close
    trail_r / trail_atr: no fixed target; the stop follows the highest high (trail_level)
               after every bar and the trade ends at that stop or at the last close
    """
    if not bars:
        return 0.0
    trig = fill + at_R * risk
    st = stop
    half = None          # R banked on the first half (partial)
    moved = False
    if mode in TRAILING:   # no fixed target: out at the trailing stop or the last close
        high = fill
        for b in bars:
            if b["l"] <= st:
                return (st - fill) / risk
            high = max(high, b["h"])
            st = trail_level(mode, fill, st, risk, high, atr_value, k)   # counts from the next bar
        return (bars[-1]["c"] - fill) / risk
    for b in bars:
        if b["l"] <= st:
            r = (st - fill) / risk
            return r if half is None else 0.5 * half + 0.5 * r
        if b["h"] >= target:
            r = (target - fill) / risk
            return r if half is None else 0.5 * half + 0.5 * r
        if not moved and mode in ("breakeven", "partial") and b["h"] >= trig:
            moved = True
            st = max(st, fill)
            if mode == "partial":
                half = (trig - fill) / risk
    r = (bars[-1]["c"] - fill) / risk
    return r if half is None else 0.5 * half + 0.5 * r


# ---------------------------------------------------------------- short side
def mirror(bars):
    """A short is a long on the price mirrored at zero: every rule above works unchanged
    (a close below the range low is a close above the mirrored range high, and so on)."""
    if isinstance(bars, dict):
        return {k: mirror_bar(b) for k, b in bars.items()}
    return [mirror_bar(b) for b in bars]


def mirror_bar(b):
    return {**b, "o": -b["o"], "h": -b["l"], "l": -b["h"], "c": -b["c"]}


def trail_level_side(side, mode, fill, stop, risk, extreme, atr_value=None, k=2.0):
    """trail_level for either side. For a short, `extreme` is the lowest low since the fill
    and the stop moves DOWN (rounded up to the cent)."""
    if side == "buy":
        return trail_level(mode, fill, stop, risk, extreme, atr_value, k)
    return -trail_level(mode, -fill, -stop, risk, -extreme, atr_value, k)


def short_features(D, spy, k, n, past):
    """Features of a breakdown, oriented so that the same filters apply:
    market_ok = SPY red since the open, rs = how much WEAKER than SPY the stock is."""
    rs = rel_strength(D, spy, k)
    ch = day_change(spy, k) if spy is not None else None
    return {"rvol": rvol(D, past, n), "market_ok": True if ch is None else ch < 0,
            "rs": None if rs is None else -rs}
