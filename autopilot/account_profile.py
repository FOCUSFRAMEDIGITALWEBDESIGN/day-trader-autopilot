"""Account profile: all limits scaled to the money you will really trade with.

    python autopilot/autopilot.py profile 2000      (or konto.bat)

The paper account at Alpaca holds $100,000; the autopilot trades as if it held only `capital`,
so the paper results are what a real account of that size would have done:

  budget                 = capital
  max loss per trade     = 1 %   of capital
  daily loss limit       = 3 %   of capital  (also: no new entries after that much realized loss)
  biggest position/order = 75 %  of capital
  cash buffer            = 10 %
  short selling          = only while the simulated account is at least $2,000 (Alpaca's minimum
                           for a margin account that may sell short); below that, long only
"""
import json
import os

SHORT_MIN_EQUITY = 2000.0


def _rw(path, update):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    update(doc)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    return doc


def limits(capital):
    c = float(capital)
    return {"budget": round(c, 2), "max_loss_per_trade_usd": max(1.0, round(c * 0.01, 2)),
            "max_daily_loss_usd": max(3.0, round(c * 0.03, 2)), "max_order_usd": round(c * 0.75, 2),
            "max_position_pct": 75, "cash_buffer_pct": 10, "min_order_usd": 5}


def apply(root, capital):
    c = float(capital)
    if c < 100:
        raise ValueError("an account under $100 cannot size a single trade")
    lim = limits(c)
    for base in ("trader", "config"):
        cfg = os.path.join(root, base, "config.json")
        if os.path.exists(cfg):
            _rw(cfg, lambda d: d.update(lim))
        ap = os.path.join(root, base, "autopilot.json")
        if os.path.exists(ap):
            _rw(ap, lambda d: d.update({
                "stop_after_daily_loss_usd": lim["max_daily_loss_usd"],
                "short_min_equity": SHORT_MIN_EQUITY,
                "account_profile": {"capital": lim["budget"], "set": "via profile"},
                "sides": "both" if c >= SHORT_MIN_EQUITY else "long"}))
    return lim
