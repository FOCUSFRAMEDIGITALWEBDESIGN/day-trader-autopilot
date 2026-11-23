"""Years of 5-minute bars from Alpaca's market-data API, saved in the same format as
Yahoo's chart JSON so the pack's Time Machine code and the backtest read them unchanged.

    python3 tuning/alpaca_data.py --years 2          # keys from .env / environment
Files: tuning/data_alpaca/<SYM>-alpaca5m.json.gz (regular session bars only).
SIP (all exchanges) is tried first; the free plan allows it for bars older than 15
minutes. If SIP is refused it falls back to IEX (one exchange: volumes are smaller,
but relative volume compares IEX with IEX, so it still works).
"""
import argparse, calendar, datetime, gzip, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
from alpaca import Alpaca, AlpacaError, load_keys  # noqa: E402

OUT = os.path.join(HERE, "data_alpaca")


def _eastern_minutes(ts):
    """Minutes after midnight New York time for a UTC epoch (DST worked out like the pack)."""
    utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)
    y = utc.year

    def nth_sunday(month, n):
        d = datetime.datetime(y, month, 1)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    dst = nth_sunday(3, 2) + datetime.timedelta(hours=7) <= utc < nth_sunday(11, 1) + datetime.timedelta(hours=6)
    local = utc - datetime.timedelta(hours=4 if dst else 5)
    return local.hour * 60 + local.minute


def to_chart(sym, bars):
    ts, o, h, l, c, v = [], [], [], [], [], []
    for b in bars:
        t = calendar.timegm(time.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S"))
        m = _eastern_minutes(t)
        if not (570 <= m < 960) or t % 300:
            continue
        ts.append(t); o.append(b["o"]); h.append(b["h"]); l.append(b["l"]); c.append(b["c"]); v.append(b["v"])
    return {"chart": {"result": [{"meta": {"symbol": sym, "dataGranularity": "5m", "source": "alpaca",
                                           "regularMarketTime": (ts[-1] + 300) if ts else 0},
                                  "timestamp": ts, "indicators": {"quote": [{"open": o, "high": h, "low": l,
                                                                             "close": c, "volume": v}]}}],
                      "error": None}}


def fetch_symbol(api, sym, start, end, feed):
    out, token = [], None
    while True:
        page, token = api.bars(sym, start, end, feed=feed, page_token=token)
        out += page
        if not token:
            return out
        time.sleep(0.35)


def fetch_history(symbols, years=2, log=print, api=None, max_age_hours=20):
    if api is None:
        key, secret = load_keys(ROOT)
        api = Alpaca(key, secret)
    os.makedirs(OUT, exist_ok=True)
    end = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=int(365 * years))).strftime("%Y-%m-%dT00:00:00Z")
    feed, ok = "sip", 0
    for sym in symbols:
        p = os.path.join(OUT, "%s-alpaca5m.json.gz" % sym)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_hours * 3600:
            ok += 1
            continue
        try:
            try:
                bars = fetch_symbol(api, sym, start, end, feed)
            except AlpacaError as e:
                if feed == "sip" and ("403" in str(e) or "422" in str(e) or "subscription" in str(e).lower()):
                    feed = "iex"
                    log("Alpaca: SIP nicht freigegeben, nutze IEX")
                    bars = fetch_symbol(api, sym, start, end, feed)
                else:
                    raise
        except AlpacaError as e:
            log("Alpaca: %s fehlgeschlagen: %s" % (sym, str(e)[:120]))
            continue
        if bars:
            with gzip.open(p, "wt", encoding="utf-8") as f:
                json.dump(to_chart(sym, bars), f)
            ok += 1
    log("Alpaca-Historie: %d von %d Aktien, %s Jahre, Feed %s" % (ok, len(symbols), years, feed))
    return ok


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--years", type=float, default=2)
    a.add_argument("--symbols", help="comma separated (default: tuning/universe.txt + SPY)")
    a = a.parse_args()
    syms = a.symbols.split(",") if a.symbols else open(os.path.join(HERE, "universe.txt")).read().split() + ["SPY"]
    fetch_history([s.upper() for s in syms], a.years)
