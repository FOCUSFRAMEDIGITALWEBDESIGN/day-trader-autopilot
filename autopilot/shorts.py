"""The short side: opening-range BREAKDOWNS.

The pack (v2) plans longs only, so the short-specific steps live here, each a mirror of
the pack's long step: the scanner's two-stage search (spark closes, then real highs and
lows), trader-exit's stop and target rules, trader-budget's four size limits, and the
portfolio bookkeeping. trader-news, the Time Machine (it takes side "sell") and
trader-risk (with allow_short on, for these orders only) are the pack's own tools.
"""
import datetime
import json
import math
import os


def _cents_up(x):
    """Round a short's stop UP to the cent (away from the entry)."""
    return math.ceil(round(x * 100, 4)) / 100.0


# ---------------------------------------------------------------- 1. the scan
def breakdown_hit(bars, n_range):
    """The first completed bar to CLOSE below the opening-range low (bars from scan.session_bars)."""
    rng = bars[:n_range]
    if len(rng) < n_range or any(b.get("h") is None or b.get("l") is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] < ol:
            closes_at = b["start"] + datetime.timedelta(minutes=5)
            hh = closes_at.hour % 12 or 12
            return {"price": round(b["c"], 2), "at": "%d:%02d %s" % (hh, closes_at.minute, "AM" if closes_at.hour < 12 else "PM"),
                    "close_min": closes_at.hour * 60 + closes_at.minute,
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((1 - b["c"] / ol) * 100, 2)}   # how far UNDER the range, in percent
    return None


def scan_breakdowns(scanmod, symbols, day, cutoff, n_range, min_price=0.0, offline=None, pause=0.3):
    """Stage 1: spark closes for every symbol (20 a request); a chart is a candidate only if a
    completed bar after the range closed at or below the lowest range close. Stage 2: the v8
    chart with real highs and lows for each candidate. -> (hits, scanned)"""
    if offline:
        data, _ = scanmod.load_offline(offline)
        data = {s: v for s, v in data.items() if s in set(symbols)}
    else:
        data, _ = scanmod.fetch_spark(list(symbols), "1d", "5m", pause)
    hits, scanned = [], 0
    for sym, series in data.items():
        bars = scanmod.session_bars(series, day, cutoff)
        if len(bars) <= n_range:
            continue
        scanned += 1
        if bars[-1]["c"] < min_price:
            continue
        low_close = min(b["c"] for b in bars[:n_range])
        if not any(b["c"] <= low_close for b in bars[n_range:]):
            continue
        if any(b.get("h") is None for b in bars) and not offline:
            try:
                full = scanmod.parse_chart(scanmod.yahoo_get(scanmod.chart_path(sym, "1d", "5m")), sym)
                bars = scanmod.session_bars(full, day, cutoff)
            except Exception:
                continue
        h = breakdown_hit(bars, n_range)
        if h:
            h["symbol"] = sym
            hits.append(h)
    return hits, scanned


# ---------------------------------------------------------------- 2. the plan (mirror of trader-exit)
def plan_short(sym, date, entry, entry_time, entry_bar, or_high, or_low, stop_rule, target, flat_by, or_minutes):
    """Stop above the entry (range midpoint or range high), rounded UP; target = entry - R x risk.
    Returns (plan, refusal)."""
    stop = _cents_up((or_high + or_low) / 2 if stop_rule == "or-mid" else or_high)
    risk = round(stop - entry, 4)
    if risk <= 0:
        return None, "the stop %.2f is not above the entry %.2f" % (stop, entry)
    if risk / entry * 100 < 0.05:
        return None, "the risk is under 0.05%% of the price: too tight to survive the spread"
    r = float(str(target).rstrip("Rr"))
    tgt = round(entry - r * risk + 1e-9, 2)
    if tgt <= 0:
        return None, "the target is not above zero"
    return {"created_by": "autopilot-short", "symbol": sym, "side": "sell", "date": date, "setup": "orb",
            "entry": entry, "entry_time": entry_time, "entry_bar": entry_bar, "interval": "5m",
            "opening_range": {"high": or_high, "low": or_low, "minutes": or_minutes},
            "stop": stop, "stop_rule": "or_mid" if stop_rule == "or-mid" else "or_high",
            "stop_label": "opening-range midpoint" if stop_rule == "or-mid" else "opening-range high",
            "target": tgt, "target_r": r, "target_label": "%gR" % r, "risk_per_share": round(risk, 2),
            "reward_risk": round((entry - tgt) / risk, 2), "flat_by": flat_by}, None


# ---------------------------------------------------------------- 3. size (mirror of trader-budget)
def size_short(cfg, plan, price=None, cash=None):
    """Whole shares, the smallest of trader-budget's four limits. A LOWER fresh price means a
    bigger risk for a short, so the entry it sizes at is the lower of the plan's and the price.
    Returns (order, refusal)."""
    sym = plan["symbol"]
    if sym in {s.upper() for s in cfg.get("blocked", [])}:
        return None, "%s is on the blocked list" % sym
    entry = min(plan["entry"], price) if price else plan["entry"]
    stop, target = plan["stop"], plan["target"]
    risk = stop - entry
    if risk <= 0 or target >= entry:
        return None, "the price %.2f is already past the stop or the target" % entry
    rr = (entry - target) / risk
    if rr + 1e-9 < float(cfg.get("min_reward_risk", 1.0)):
        return None, "reward:risk %.2f is under %.2f" % (rr, float(cfg.get("min_reward_risk", 1.0)))
    budget = float(cfg.get("budget") or 0)
    room = budget * (1 - float(cfg.get("cash_buffer_pct", 10)) / 100)
    if cash is not None:
        room = min(room, cash)
    limits = {
        "max loss": math.floor(float(cfg.get("max_loss_per_trade_usd", 50)) / risk + 1e-9),
        "position cap": math.floor(budget * float(cfg.get("max_position_pct", 75)) / 100 / entry + 1e-9),
        "max order": math.floor(float(cfg.get("max_order_usd", 4000)) / entry + 1e-9),
        "cash": math.floor(room / entry + 1e-9),
    }
    qty = min(limits.values())
    decided = min(limits, key=limits.get)
    if qty < 1:
        return None, "zero shares fit (%s)" % decided
    if qty * entry < float(cfg.get("min_order_usd", 5)):
        return None, "the order is under the minimum"
    loss = round(qty * risk, 2)
    return {"symbol": sym, "side": "sell", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop,
            "why": "opening-range breakdown on %s: short %d shares, bound by your %s (%d x %.2f = $%.2f at the stop)"
                   % (sym, qty, decided, qty, risk, loss),
            "plan": {"entry": round(entry, 2), "stop": stop, "target": target, "risk_per_share": round(risk, 2),
                     "max_loss": loss, "setup": "orb-short", "entry_time": plan.get("entry_time"),
                     "flat_by": plan.get("flat_by")}}, None


def orders_doc(order, now_utc, paper=True, note="Short entry by the autopilot."):
    return {"created_by": "autopilot-short", "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "proposed", "paper": paper, "note": note,
            "source": "trader/plan.json (%s orb short %s)" % (order["symbol"], now_utc.date().isoformat()),
            "orders": [order]}


def risk_config(cfg, path):
    """trader-risk's config for SHORT orders only: the user's limits with allow_short on."""
    c = dict(cfg)
    c["allow_short"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    return path


# ---------------------------------------------------------------- 4. the books
def apply_fill(path, cfg, sym, side, qty, price, date):
    """Record a short sale (side sell, shares go negative) or a cover (side buy) in
    trader/portfolio.json, so trader-risk sees the short and treats the cover as an exit."""
    try:
        with open(path, encoding="utf-8") as f:
            pf = json.load(f)
    except (OSError, ValueError):
        pf = {"cash": float(cfg.get("budget") or 0), "positions": []}
    qty, price = float(qty), float(price)
    pos = next((p for p in pf.setdefault("positions", []) if str(p.get("ticker", "")).upper() == sym), None)
    if pos is None:
        pos = {"ticker": sym, "shares": 0.0, "lots": [], "short": True}
        pf["positions"].append(pos)
    if side == "sell":
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        pos.setdefault("lots", []).append({"id": "%s-S%d" % (sym, len(pos["lots"]) + 1), "date": date,
                                           "shares": -qty, "cost": -round(qty * price, 2)})
        pf["cash"] = round(float(pf.get("cash", 0)) + qty * price, 2)
    else:
        pos["shares"] = round(float(pos["shares"]) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - qty * price, 2)
        if abs(pos["shares"]) <= 1e-9:
            pf["positions"].remove(pos)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pf, f, indent=1)
    os.replace(tmp, path)
    return pf
