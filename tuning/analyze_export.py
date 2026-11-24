"""Read a weekly export and say what worked.

    python3 tuning/analyze_export.py autopilot-export-2026-11-30.zip [--candles DIR] [--out report.md]

1. The real paper trades, grouped every useful way (autopilot/stats.py, the same as the dashboard).
2. Counterfactuals: every candidate the bot saw, also the ones it rejected, replayed on the real
   5-minute candles of that day with the plan it would have had. So each filter can be judged:
   "the Time Machine rejected 23 setups; traded, they would have averaged -0.31 R" means the
   gate saved money, a positive number means it cost money.
3. Fill quality: the real fills against the signal price (slippage, in R).
Candles: Yahoo's last 60 days (cached in tuning/data_export/), or --candles DIR with <SYM>.json(.gz).
"""
import argparse, gzip, json, os, statistics, sys, tempfile, time, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import backtest as bt  # noqa: E402
import features as fx  # noqa: E402
import stats  # noqa: E402

tm = bt.tm
FLAT = bt.FLAT_SLOT
CACHE = os.path.join(HERE, "data_export")
STAGE_DE = {"scanner": "Scan (zu spät / zu weit)", "news": "News", "entry": "Entry / Filter", "exit": "Exit-Plan",
            "timemachine": "Time Machine", "budget": "Budget / Preis", "limits": "Tageslimits", "risk": "Risk",
            "broker": "Broker"}


def candles(sym, candle_dir=None):
    """{date: [78 bars]} of full sessions."""
    paths = []
    if candle_dir:
        paths = [os.path.join(candle_dir, n) for n in (sym + ".json", sym + ".json.gz", sym + "-60d5m.json.gz")]
    else:
        os.makedirs(CACHE, exist_ok=True)
        p = os.path.join(CACHE, sym + "-60d5m.json.gz")
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 12 * 3600:
            for host in ("query2", "query1"):
                try:
                    raw = urllib.request.urlopen(urllib.request.Request(
                        "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=5m&range=60d" % (host, sym),
                        headers={"User-Agent": "day-trader-skill/2.0"}), timeout=30).read()
                    if len(raw) > 5000:
                        with gzip.open(p, "wb") as f:
                            f.write(raw)
                        break
                except Exception:
                    time.sleep(5)
            time.sleep(1)
        paths = [p]
    for p in paths:
        if os.path.exists(p):
            try:
                op = gzip.open if p.endswith(".gz") else open
                with op(p, "rt", encoding="utf-8") as f:
                    text = f.read()
                res = tm.chart_result(text, sym)
                import datetime
                _, bars, _ = tm.parse_bars(res, datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
                out = {}
                for d, D in tm.split_sessions(bars).items():
                    n = 0
                    while n in D:
                        n += 1
                    out[d] = [D[i] for i in range(n)]          # contiguous from 9:30 (half days and today too)
                return out
            except Exception:
                pass
    return {}


def replay(c, D, settings):
    """R of a candidate traded with its plan on the day's real bars (fill = next bar's open)."""
    h = c.get("scan") or {}
    k = h.get("bar_index")
    if k is None or not D or k + 1 > FLAT or h.get("or_high") is None:
        return None
    side = c.get("side", "buy")
    plan = c.get("plan") or {}
    R = float(str(settings.get("target", "2R")).rstrip("Rr"))
    oh, ol, sig = h["or_high"], h["or_low"], h["price"]
    if side == "buy":
        stop = plan.get("stop") or tm.cents_away((oh + ol) / 2 if settings.get("stop", "or-mid") == "or-mid" else ol, True)
        bars, fill = D[k + 1:FLAT + 1], D[k + 1]["o"]
        sgn = 1
    else:
        stop = plan.get("stop") or -tm.cents_away(-((oh + ol) / 2 if settings.get("stop", "or-mid") == "or-mid" else oh), True)
        bars, fill = fx.mirror(D[k + 1:FLAT + 1]), -D[k + 1]["o"]
        sig, stop = -sig, -stop
        sgn = -1
    risk = sig - stop
    if risk <= 0:
        return None
    target = sig + R * risk
    mode = settings.get("exit_mode", "fixed")
    a14 = fx.atr(D[:k + 1] if sgn > 0 else fx.mirror(D[:k + 1]))
    if fill <= stop or fill >= target:
        return (stop - fill) / risk if fill <= stop else (target - fill) / risk
    return fx.simulate_exit(bars, fill, stop, target, risk, mode, atr_value=a14, k=float(settings.get("exit_atr_k", 2)))


def first_stop(c):
    for s in ("scanner", "news", "entry", "exit", "timemachine", "budget", "limits", "risk", "broker"):
        if (c.get("stages") or {}).get(s, {}).get("ok") is False:
            return s
    return "traded" if c.get("result") == "ORDER PLACED" else None


def fmt(rs):
    if not rs:
        return "–"
    return "n=%d, Ø %+.2f R, Treffer %.0f %%" % (len(rs), statistics.mean(rs), 100 * sum(1 for r in rs if r > 0) / len(rs))


def analyze(zip_path, candle_dir=None):
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    ap_dir = os.path.join(tmp, "trader", "autopilot")
    rep = stats.report(ap_dir)
    settings = json.load(open(os.path.join(tmp, "trader", "autopilot.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(tmp, "trader", "autopilot.json")) else {}
    _, cands, learning = stats.load(ap_dir)
    lines = ["# Auswertung %s" % os.path.basename(zip_path), ""]
    o = rep["overall"]
    lines += ["## 1. Echte Paper-Trades", ""]
    if o.get("n"):
        lines += ["- %d Trades an %d Tagen, Trefferquote %s %%, Ø %+.2f R, Summe $%+.2f, Profit-Faktor %s, max. Rückgang $%.2f"
                  % (o["n"], len(rep["days"]), o["win_pct"], o["avg_R"] or 0, o["pnl"], o["profit_factor"], o["max_drawdown"]),
                  "- Backtest erwartete Ø %s R, live %s R (Differenz %s)" % (rep["backtest_expectation_R"], o["avg_R"],
                                                                          rep["vs_backtest"]), ""]
        for g in rep["groups"]:
            lines.append("**%s**" % g["title"])
            for r in g["rows"]:
                lines.append("- %s: %d Trades, %s %% Treffer, Ø %+.2f R, $%+.2f%s" % (
                    r["label"], r["n"], r["win_pct"], r["avg_R"] or 0, r["pnl"], "" if r["enough"] else " (zu wenige)"))
            lines.append("")
    else:
        lines += ["Noch keine geschlossenen Trades.", ""]

    # counterfactuals
    by_sym = {}
    for c in cands:
        by_sym.setdefault(c["symbol"], []).append(c)
    groups, traded_sim, slip = {}, [], []
    real = {(t.get("date"), t.get("symbol")): t for t in stats.load(ap_dir)[0]}
    for sym, cs in by_sym.items():
        days = candles(sym, candle_dir)
        for c in cs:
            st = first_stop(c)
            r = replay(c, days.get(c["date"]), settings)
            if st is None or r is None:
                continue
            groups.setdefault(st, []).append(r)
            if st == "traded":
                t = real.get((c["date"], sym))
                if t and t.get("fill_price") and (c.get("plan") or {}).get("risk_per_share"):
                    sg = 1 if c.get("side", "buy") == "buy" else -1
                    slip.append(sg * (t["fill_price"] - c["plan"]["entry"]) / c["plan"]["risk_per_share"])
    lines += ["## 2. Was die Filter bewirkt haben (Gegenprobe auf echten Kerzen)", "",
              "Jeder Kandidat mit dem Plan nachgespielt, den er gehabt hätte. Negativ bei einer Sperre = der Filter hat "
              "Geld gespart, positiv = er hat Gewinn verhindert.", ""]
    for st in ["traded"] + [s for s in STAGE_DE if s in groups]:
        if st in groups:
            lines.append("- %s: %s" % ("**Gehandelt**" if st == "traded" else "Gestoppt bei " + STAGE_DE[st], fmt(groups[st])))
    lines += ["", "## 3. Ausführung", ""]
    lines.append("- Slippage beim Einstieg: %s" % (("Ø %+.3f R (positiv = schlechter als das Signal)" % statistics.mean(slip))
                                                 if slip else "keine Daten"))
    lines += ["", "## 4. Letzter Lernlauf", ""]
    if learning:
        L = learning[-1]
        lines.append("- %s: %s; aktueller Plan im Test %s" % (L.get("run_at"), L.get("result"), L.get("current_eval")))
    return "\n".join(lines) + "\n", {"report": rep, "counterfactual": {k: stats_of(v) for k, v in groups.items()},
                                     "slippage_R": statistics.mean(slip) if slip else None}


def stats_of(rs):
    return {"n": len(rs), "avg_R": round(statistics.mean(rs), 3) if rs else None}


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("zip")
    a.add_argument("--candles")
    a.add_argument("--out")
    a = a.parse_args()
    text, _ = analyze(a.zip, a.candles)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    print(text)
