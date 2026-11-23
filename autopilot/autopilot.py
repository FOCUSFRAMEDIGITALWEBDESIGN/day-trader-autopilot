#!/usr/bin/env python3
"""Day-trader autopilot: runs the day-trader-skill pipeline on its own, on an Alpaca PAPER account.

    scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
    alerts / monitor: stop, target, flat by the close                              (while it is open)

Every step is the pack's own tool (trader-*/…py), run exactly as the skills run it,
reading and writing the same trader/ files. The only gate that is replaced is the
human "yes" in trader-broker: the autopilot records an automatic approval, marked as
such in trader/state/broker.json, and only for a PAPER account. trader-risk must
still PASS every exact order, and the Time Machine must show an edge.

    python3 autopilot/autopilot.py run                  # the daemon (real clock, loops forever)
    python3 autopilot/autopilot.py tick                 # one pass now
    python3 autopilot/autopilot.py tick --now "2026-10-19 10:15" --sim   # replay (offline files + mock broker)
    python3 autopilot/autopilot.py status               # one line
    python3 autopilot/autopilot.py pause | resume       # stop / allow new entries (exits keep running)
"""
import argparse
import datetime
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
from alpaca import Alpaca, AlpacaError, load_keys  # noqa: E402

T = os.path.join(ROOT, "trader")
AP = os.path.join(T, "autopilot")
PY = sys.executable or "python3"
AUTO_REPLY = "AUTOPILOT: automatic approval on the PAPER account (enabled by the user in trader/autopilot.json)"

TOOLS = {
    "scan": "trader-scanner/scan.py", "news": "trader-news/news.py", "entry": "trader-entry/entry.py",
    "exit": "trader-exit/exit.py", "tm": "trader-timemachine/timemachine.py", "size": "trader-budget/size.py",
    "risk": "trader-risk/check.py", "ticket": "trader-broker/ticket.py", "alerts": "trader-alerts/alerts.py",
}


def _mod(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


clockmod = _mod("ap_alerts", TOOLS["alerts"])
entrymod = _mod("ap_entry", TOOLS["entry"])
tmmod = _mod("ap_tm", TOOLS["tm"])
import features as fx  # noqa: E402  (shared with the backtest and the learner)
ticketmod = _mod("ap_ticket", TOOLS["ticket"])

DEFAULTS = {
    "enabled": True,
    "auto_approve_paper": True,
    "symbols": [],
    "premarket_scan": True,
    "premarket_add": 25,
    "premarket_min_price": 10,
    "premarket_min_volume": 2000000,
    "scan_min_price": 10,
    "or_minutes": 15,
    "stop": "or-mid",
    "target": "2R",
    "gap_filter": True,
    "min_volume_x": None,
    "max_pct_over": 0.75,
    "min_risk_pct": 0.15,
    "max_risk_pct": 2.5,
    "last_entry": "11:30",
    "max_signal_age_min": 6,
    "max_candidates_per_bar": 4,
    "max_open_positions": 2,
    "news_hours": 16,
    "news_block_tags": ["halt"],
    "news_block_important_8k": True,
    "tm_replays": 10000,
    "tm_seed": 19,
    "tm_min_edge_pts": 0.0,
    "tm_min_avg_R": 0.0,
    "tm_hist_min_trades": 8,
    "tm_hist_min_avg_R": None,
    "stop_after_daily_loss_usd": None,
    "rvol_min": 0,
    "market_filter": "none",
    "rs_min": None,
    "exit_mode": "fixed",
    "exit_at_R": 1.0,
    "learn_years": 2,
    "learn_weekly": True,
    "strategy_version": 1,
    "loop_seconds": 20,
    "scan_delay_seconds": 25,
    "sim": {},
}


# ---------------------------------------------------------------- small helpers

def rj(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def wj(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, path)


def hm(text):
    """'10:15', '10:15 AM', '9:50 AM' -> minutes after midnight."""
    t = str(text).strip().upper()
    pm, am = t.endswith("PM"), t.endswith("AM")
    t = t.replace("AM", "").replace("PM", "").strip()
    h, m = [int(x) for x in t.split(":")[:2]]
    if pm and h != 12:
        h += 12
    if am and h == 12:
        h = 0
    return h * 60 + m


class Pilot:
    def __init__(self, now=None, sim=False, broker_url=None):
        os.makedirs(AP, exist_ok=True)
        self.fixed_now = now
        self.sim = sim
        self.cfg = rj(os.path.join(T, "config.json"), {}) or {}
        user = rj(os.path.join(T, "autopilot.json"), {}) or {}
        self.ap = dict(DEFAULTS)
        self.ap.update(user)
        if not self.cfg.get("paper", True):
            raise SystemExit("trader/config.json has \"paper\": false. The autopilot only trades paper accounts.")
        key, secret = load_keys(ROOT)
        if sim:
            key, secret = key or "SIM", secret or "SIM"
        self.broker = Alpaca(key, secret, base_url=broker_url)
        self.state_path = os.path.join(AP, "state.json")
        self.state = rj(self.state_path, {}) or {}
        self.last_error = None

    # ------------------------------------------------------------ clock
    def now_utc(self):
        if self.fixed_now:
            return clockmod.parse_now(self.fixed_now)
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    def ny(self):
        return clockmod.to_eastern(self.now_utc())

    def now_arg(self):
        return ["--now", self.fixed_now] if self.fixed_now else []

    # ------------------------------------------------------------ events
    def event(self, level, msg, **data):
        rec = {"t": self.ny().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": msg}
        if data:
            rec["data"] = data
        with open(os.path.join(AP, "events.jsonl"), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        print("[%s] %-5s %s" % (rec["t"], level, msg), flush=True)

    # ------------------------------------------------------------ tools
    def tool(self, name, *args, now=True, offline=None, timeout=600):
        cmd = [PY, os.path.join(ROOT, TOOLS[name])] + [str(a) for a in args]
        if now and self.fixed_now:
            cmd += ["--now", self.fixed_now if name != "news" else
                    self.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")]
        off = (self.ap.get("sim") or {}).get(offline or name) if self.sim else None
        if off:
            cmd += ["--offline", os.path.join(ROOT, off)]
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "%s timed out after %ds" % (name, timeout)
        return p.returncode, p.stdout, p.stderr

    def tool_json(self, name, *args, **kw):
        code, out, err = self.tool(name, *(list(args) + ["--json"]), **kw)
        try:
            return code, json.loads(out), err
        except ValueError:
            return code, None, (err or out).strip().splitlines()[-1:] and (err or out).strip().splitlines()[-1]

    # ------------------------------------------------------------ day state
    def day(self):
        today = self.ny().date().isoformat()
        if self.state.get("date") != today:
            prev = self.state
            if prev.get("date"):
                hist = rj(os.path.join(AP, "history.json"), []) or []
                hist = [h for h in hist if h.get("date") != prev["date"]]
                hist.append({"date": prev["date"], "trades": prev.get("trades", []),
                             "funnel": prev.get("funnel", {}), "realized": prev.get("realized", 0.0)})
                wj(os.path.join(AP, "history.json"), hist[-400:])
            self.state = {"date": today, "symbols": [], "premarket_done": False, "last_scan_bar": None,
                          "candidates": {}, "trades": [], "funnel": {}, "scans": [], "realized": 0.0,
                          "flattened": False, "paused": self.state.get("paused", False)}
            self.event("info", "new trading day %s" % today)
        return self.state

    def save(self):
        wj(self.state_path, self.state)

    def paused(self):
        return bool(self.state.get("paused")) or os.path.exists(os.path.join(AP, "PAUSE")) \
            or not self.ap.get("enabled", True)

    def bump(self, stage, n=1):
        f = self.state.setdefault("funnel", {})
        f[stage] = f.get(stage, 0) + n

    # ------------------------------------------------------------ the tick
    def tick(self):
        st = clockmod.market_status(self.now_utc())
        d = self.day()
        local = self.ny()
        mins = local.hour * 60 + local.minute
        phase = "closed"
        try:
            self.sync_account()
            if st.get("opens"):
                close_m = hm(st["closes"])
                flat_m = min(hm(self.cfg.get("flat_by", "15:55")), close_m - 5)
                if mins < hm(st["opens"]):
                    phase = "premarket"
                    if mins >= 9 * 60 and not d["premarket_done"]:
                        self.premarket()
                elif mins < close_m:
                    if not d["premarket_done"]:
                        self.premarket()
                    self.monitor()
                    if mins >= flat_m:
                        phase = "flatten"
                        self.flatten_all()
                    else:
                        phase = "trading"
                        self.maybe_scan(local, st)
                else:
                    phase = "after close"
                    self.monitor()
        except Exception as e:  # keep the daemon alive; report it
            self.last_error = "%s: %s" % (type(e).__name__, e)
            self.event("error", self.last_error, trace=traceback.format_exc()[-1500:])
        try:
            self.maybe_learn(phase, mins)
        except Exception as e:
            self.event("error", "learner could not start: %s" % e)
        self.write_status(st, phase)
        self.save()

    # ------------------------------------------------------------ weekly learning
    def maybe_learn(self, phase, mins):
        """Start autopilot/learn.py outside trading hours, at most once every 6.5 days."""
        if self.sim or self.fixed_now or not self.ap.get("learn_weekly", True):
            return
        proc = getattr(self, "_learn_proc", None)
        if proc is not None and proc.poll() is None:
            return
        quiet = phase in ("after close", "closed") or (phase == "premarket" and mins < 8 * 60 + 30)
        if not quiet:
            return
        hist = rj(os.path.join(AP, "learning.json"), []) or []
        last = self.state.get("learn_started") or (hist[-1].get("run_at") if hist else None)
        if last:
            try:
                age = datetime.datetime.now() - datetime.datetime.strptime(last[:16], "%Y-%m-%d %H:%M")
                if age < datetime.timedelta(days=6.5):
                    return
            except ValueError:
                pass
        self.state["learn_started"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        log = open(os.path.join(AP, "learn.log"), "a", encoding="utf-8")
        self._learn_proc = subprocess.Popen([PY, os.path.join(ROOT, "autopilot", "learn.py")], cwd=ROOT,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        self.event("info", "Lernen gestartet: neue Daten laden und Strategie prüfen (dauert einige Minuten)")

    # ------------------------------------------------------------ account
    def sync_account(self):
        try:
            acct = self.broker.account()
            pos = self.broker.positions()
        except AlpacaError as e:
            self.last_error = str(e)
            return
        self.state["account"] = {k: acct.get(k) for k in (
            "account_number", "status", "equity", "last_equity", "cash", "buying_power", "currency",
            "daytrade_count", "pattern_day_trader")}
        self.state["account"]["account_number"] = str(acct.get("account_number", ""))[:2] + "…" + \
            str(acct.get("account_number", ""))[-3:]
        self.state["positions"] = [{k: p.get(k) for k in (
            "symbol", "qty", "avg_entry_price", "current_price", "market_value", "unrealized_pl",
            "unrealized_plpc", "side")} for p in pos]
        # equity curve, one point per 5 minutes
        eq = os.path.join(AP, "equity.jsonl")
        stamp = self.ny().strftime("%Y-%m-%d %H:%M")
        key = stamp[:-1] + str(int(stamp[-1]) // 5 * 5)
        if self.state.get("_eq_key") != key:
            self.state["_eq_key"] = key
            with open(eq, "a") as f:
                f.write(json.dumps({"t": stamp, "equity": float(acct.get("equity") or 0)}) + "\n")
        # the pack's portfolio.json: the budget's own cash, at most the account's cash
        pf_path = os.path.join(T, "portfolio.json")
        if not self.state.get("portfolio_synced"):
            budget = float(self.cfg.get("budget") or 0)
            cash = min(budget, float(acct.get("cash") or 0)) if budget else float(acct.get("cash") or 0)
            held = [{"ticker": p["symbol"], "shares": float(p["qty"]),
                     "lots": [{"id": p["symbol"] + "-1", "date": self.state["date"], "shares": float(p["qty"]),
                               "cost": round(float(p["qty"]) * float(p["avg_entry_price"]), 2)}]}
                    for p in pos if float(p.get("qty") or 0) > 0]
            cash -= sum(l["lots"][0]["cost"] for l in held)
            wj(pf_path, {"cash": round(cash, 2), "positions": held,
                         "note": "synced from the Alpaca paper account by the autopilot at the start of the day"})
            self.state["portfolio_synced"] = True

    # ------------------------------------------------------------ before the open
    def premarket(self):
        d = self.state
        syms = [s.upper() for s in self.ap.get("symbols", [])]
        if self.ap.get("premarket_scan") and not self.sim:
            uni = os.path.join(T, "universe.txt")
            if not os.path.exists(uni) or time.time() - os.path.getmtime(uni) > 7 * 86400:
                self.event("info", "scanner: refreshing the stock universe (Nasdaq Trader)")
                self.tool("scan", "universe", now=False)
            self.event("info", "scanner: pre-market daily scan of the whole universe")
            code, js, err = self.tool_json("scan", "daily", "--min-price", self.ap["premarket_min_price"],
                                           "--min-volume", self.ap["premarket_min_volume"], "--no-write",
                                           timeout=900)
            if js and js.get("hits"):
                add = [h["symbol"] for h in js["hits"][: int(self.ap["premarket_add"])]]
                d["premarket_hits"] = js["hits"][: int(self.ap["premarket_add"])]
                syms += [s for s in add if s not in syms]
                self.event("info", "pre-market scan: %d hits, added the %d most traded to today's list"
                           % (len(js["hits"]), len(add)))
            else:
                self.event("warn", "pre-market scan gave nothing: %s" % (err or "no hits"))
        blocked = {s.upper() for s in self.cfg.get("blocked", [])}
        d["symbols"] = [s for s in syms if s not in blocked]
        d["premarket_done"] = True
        self.event("info", "today's scan list: %d symbols" % len(d["symbols"]))

    # ------------------------------------------------------------ every 5-minute bar
    def maybe_scan(self, local, st):
        d = self.state
        mins = local.hour * 60 + local.minute
        bar = (mins - 570) // 5
        sec_into = (mins - 570) % 5 * 60 + local.second
        if d.get("last_scan_bar") == bar or (sec_into < int(self.ap["scan_delay_seconds"]) and not self.fixed_now):
            return
        d["last_scan_bar"] = bar
        if mins < 570 + int(self.ap["or_minutes"]) + 5:
            return  # the range is still forming
        if mins > hm(self.ap.get("last_entry") or self.cfg.get("no_entries_after", "15:30")):
            return
        if self.paused():
            return
        lim = self.ap.get("stop_after_daily_loss_usd")
        if lim and d.get("realized", 0) <= -abs(float(lim)):
            return
        open_n = sum(1 for t in d["trades"] if t.get("status") in ("open", "pending"))
        if open_n >= int(self.ap["max_open_positions"]):
            return
        entries = sum(1 for t in d["trades"] if t.get("status") != "rejected")
        if entries >= int(self.cfg.get("max_trades_per_day", 3)):
            return
        self.scan_and_trade(mins)

    def scan_and_trade(self, mins):
        d = self.state
        if not d["symbols"]:
            return
        args = ["orb", "--symbols", ",".join(d["symbols"]), "--or-minutes", self.ap["or_minutes"],
                "--min-price", self.ap["scan_min_price"], "--show", 200]
        code, js, err = self.tool_json("scan", *args, timeout=400)
        if not js:
            d["scans"].append({"t": self.ny().strftime("%H:%M"), "hits": 0, "note": err})
            return
        hits = js.get("hits", [])
        d["scans"].append({"t": self.ny().strftime("%H:%M"), "scanned": js.get("scanned"), "hits": len(hits)})
        d["scans"] = d["scans"][-100:]
        fresh = []
        for h in hits:
            sym = h["symbol"]
            if sym in d["candidates"]:
                continue
            age = mins - hm(h["at"])
            c = {"symbol": sym, "first_seen": self.ny().strftime("%H:%M"), "scan": h, "stages": {}}
            d["candidates"][sym] = c
            self.bump("scanner")
            if age > int(self.ap["max_signal_age_min"]):
                self.reject(c, "scanner", "broke out at %s, %d min ago: a late entry is a different trade"
                            % (h["at"], age))
                continue
            if self.ap.get("max_pct_over") and h.get("pct_over", 0) > float(self.ap["max_pct_over"]):
                self.reject(c, "scanner", "closed %.2f%% over the range, more than %.2f%%: extended"
                            % (h["pct_over"], float(self.ap["max_pct_over"])))
                continue
            c["stages"]["scanner"] = {"ok": True, "note": "closed %.2f%% over the range at %s" % (h["pct_over"], h["at"])}
            fresh.append(c)
        if fresh:
            self.event("info", "scan %s: %d breakouts, %d fresh: %s" % (self.ny().strftime("%H:%M"), len(hits),
                       len(fresh), ", ".join(c["symbol"] for c in fresh)))
        for c in fresh[: int(self.ap["max_candidates_per_bar"])]:
            open_n = sum(1 for t in d["trades"] if t.get("status") in ("open", "pending"))
            entries = sum(1 for t in d["trades"] if t.get("status") != "rejected")
            if open_n >= int(self.ap["max_open_positions"]) or entries >= int(self.cfg.get("max_trades_per_day", 3)):
                self.reject(c, "limits", "the day's position / trade limit is reached")
                continue
            self.pipeline(c)
        for c in fresh[int(self.ap["max_candidates_per_bar"]):]:
            self.reject(c, "scanner", "more fresh breakouts than max_candidates_per_bar this bar")

    def reject(self, c, stage, why):
        c["stages"][stage] = {"ok": False, "note": why}
        c["result"] = "skipped at %s" % stage
        c["why"] = why

    def ok(self, c, stage, note, **data):
        c["stages"][stage] = dict({"ok": True, "note": note}, **data)
        self.bump(stage)

    # ------------------------------------------------------------ one candidate, the whole chain
    def pipeline(self, c):
        sym = c["symbol"]
        # 2. news
        args = ["--tickers", sym, "--hours", self.ap["news_hours"], "--dry-run"]
        if not (self.cfg.get("sec_user_agent") and "@" in str(self.cfg.get("sec_user_agent"))
                and "example.com" not in str(self.cfg.get("sec_user_agent"))):
            args.append("--no-sec")
        code, js, err = self.tool_json("news", *args, timeout=120)
        items = (js or {}).get("items", [])
        bad = [i for i in items if set(i.get("tags", [])) & set(self.ap["news_block_tags"])]
        if self.ap.get("news_block_important_8k"):
            bad += [i for i in items if i.get("kind") == "filing" and i.get("important")]
        move = next((m for m in (js or {}).get("moves", []) if m.get("ticker") == sym), None)
        c["news"] = [{"time": i.get("time"), "title": i.get("title"), "tags": i.get("tags"), "link": i.get("link"),
                      "kind": i.get("kind")} for i in items[:6]]
        if bad:
            return self.reject(c, "news", "blocked by news: %s" % bad[0].get("title", "")[:120])
        self.ok(c, "news", "%d items in %sh, none blocking%s" % (
            len(items), self.ap["news_hours"], ("; %+.1f%% vs yesterday" % move["pct"]) if move and
            move.get("pct") is not None else ""))

        # 3. entry
        eargs = ["check", sym, "--write", "--or-minutes", self.ap["or_minutes"]]
        if self.ap.get("min_volume_x"):
            eargs += ["--min-volume-x", self.ap["min_volume_x"]]
        code, js, err = self.tool_json("entry", *eargs, timeout=120)
        if not js or js.get("status") != "triggered":
            return self.reject(c, "entry", "entry says %s: %s" % ((js or {}).get("status", "error"),
                                                                    (js or {}).get("reason") or err))
        age = js.get("minutes_since") or 0
        if age > int(self.ap["max_signal_age_min"]):
            return self.reject(c, "entry", "triggered %s min ago: too late" % age)
        trig = js["trigger"]
        if self.ap.get("gap_filter"):
            gap = self.day_open_vs_prev(sym)
            if gap is not None and gap < 0:
                return self.reject(c, "entry", "opened %.2f%% under yesterday's close: gap filter" % gap)
        if self.ap.get("rvol_min") or self.ap.get("market_filter") not in (None, "none") \
                or self.ap.get("rs_min") is not None:
            f = self.live_features(sym, int(trig["bar_index"]), int(self.ap["or_minutes"]) // 5)
            c["features"] = f
            ok, why = fx.passes(f, self.ap)
            if not ok:
                return self.reject(c, "entry", "filter: " + why)
        self.ok(c, "entry", "triggered %s at %.2f (%+.2f%% over %.2f)%s" % (
            trig["time"], trig["price"], trig["pct_over"], js["level"], self.feature_note(c.get("features"))),
            trigger=trig, opening_range=js.get("opening_range"))

        # 4. exit plan
        code, js, err = self.tool_json("exit", "plan", "--stop", self.ap["stop"], "--target", self.ap["target"],
                                       "--write", now=True, timeout=120)
        if code != 0 or not js or not js.get("stop"):
            return self.reject(c, "exit", "exit planner refused: %s" % ((js or {}).get("error") or err))
        plan = js
        risk_pct = plan["risk_per_share"] / plan["entry"] * 100
        if not (float(self.ap["min_risk_pct"]) <= risk_pct <= float(self.ap["max_risk_pct"])):
            return self.reject(c, "exit", "risk %.2f%% of the price is outside %.2f-%.2f%%" % (
                risk_pct, float(self.ap["min_risk_pct"]), float(self.ap["max_risk_pct"])))
        c["plan"] = {k: plan.get(k) for k in ("entry", "stop", "target", "risk_per_share", "reward_risk",
                                              "target_r", "stop_label", "flat_by", "entry_time")}
        self.ok(c, "exit", "stop %.2f, target %.2f (%sR), risk %.2f a share" % (
            plan["stop"], plan["target"], plan.get("target_r"), plan["risk_per_share"]))

        # 5. time machine
        code, js, err = self.tool_json("tm", "run", "--write", "--until", self.cfg.get("flat_by", "15:55"),
                                       "--replays", self.ap["tm_replays"], "--seed", self.ap["tm_seed"], timeout=300)
        if not js or not js.get("mc"):
            return self.reject(c, "timemachine", "time machine could not run: %s" % err)
        mc, hist = js["mc"], js.get("history", {})
        R = float(mc.get("r_at_target") or plan.get("target_r") or 1)
        sided = (mc["p_target"] + mc["p_stop"]) or 1e-9
        edge = (mc["p_target"] / sided - 1 / (1 + R)) * 100
        c["tm"] = {"p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
                   "p_green": mc.get("p_green"), "avg_R": mc["avg_R"], "edge_pts": round(edge, 1),
                   "median_close": mc.get("median_close"), "p5": mc.get("p5_close"), "p95": mc.get("p95_close"),
                   "verdict": js.get("verdict"), "seed": mc.get("seed"),
                   "hist": {k: hist.get(k) for k in ("sessions", "triggered", "target", "stop", "close", "avg_R")},
                   "bands": mc.get("bands")}
        why = None
        if edge < float(self.ap["tm_min_edge_pts"]):
            why = "edge %.1f pts under the %.1f needed (%s)" % (edge, float(self.ap["tm_min_edge_pts"]),
                                                                 js.get("verdict"))
        elif mc["avg_R"] < float(self.ap["tm_min_avg_R"]):
            why = "average R %+.3f under %+.3f" % (mc["avg_R"], float(self.ap["tm_min_avg_R"]))
        elif self.ap.get("tm_hist_min_avg_R") is not None and \
                (hist.get("triggered") or 0) >= int(self.ap["tm_hist_min_trades"]) and \
                (hist.get("avg_R") if hist.get("avg_R") is not None else 0) < float(self.ap["tm_hist_min_avg_R"]):
            why = "the same rule on the last %d sessions averaged %+.3f R" % (hist["sessions"], hist["avg_R"])
        if why:
            return self.reject(c, "timemachine", why)
        self.ok(c, "timemachine", "target %.1f%% / stop %.1f%% / neither %.1f%%, edge %+.1f pts, avg R %+.3f" % (
            100 * mc["p_target"], 100 * mc["p_stop"], 100 * mc["p_neither"], edge, mc["avg_R"]))

        # fresh price for budget and risk (the higher price means a bigger risk, fewer shares)
        last = None if self.sim else self.broker.latest_trade(sym)   # Alpaca, real time
        code = 0 if (self.sim or last) else self.tool("alerts", "quote", sym, "--write", now=False, timeout=60)[0]
        if last or code != 0:
            last = last or self.broker.latest_trade(sym)
            if last:
                pr = rj(os.path.join(T, "prices.json"), {}) or {}
                pr.setdefault("prices", {})[sym] = last
                pr["as_of"] = self.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
                wj(os.path.join(T, "prices.json"), pr)
        px = ((rj(os.path.join(T, "prices.json"), {}) or {}).get("prices") or {}).get(sym)
        last = float(px["price"] if isinstance(px, dict) else px) if px else None
        if last and (last >= plan["target"] or last <= plan["stop"]):
            return self.reject(c, "budget", "price %.2f is already past the %s" % (
                last, "target" if last >= plan["target"] else "stop"))

        # 6. budget (a stale orders.json must never be mistaken for this one)
        try:
            os.remove(os.path.join(T, "orders.json"))
        except OSError:
            pass
        code, out, err = self.tool("size", "--write", timeout=60)
        orders = rj(os.path.join(T, "orders.json"), {}) or {}
        if code != 0 or not orders.get("orders") or (orders.get("source", "").find(sym) < 0):
            return self.reject(c, "budget", "budget refused: %s" % ((err or out).strip().splitlines() or ["?"])[-1])
        o = orders["orders"][0]
        self.ok(c, "budget", "%s shares, max loss $%.2f" % (o["qty"], o["plan"]["max_loss"]),
                qty=o["qty"], max_loss=o["plan"]["max_loss"])

        # 7. risk
        code, js, err = self.tool_json("risk", timeout=60)
        r = ((js or {}).get("results") or [{}])[0]
        if r.get("verdict") != "PASS":
            return self.reject(c, "risk", "BLOCK: %s" % "; ".join(r.get("reasons") or [str(err)]))
        self.ok(c, "risk", "PASS: " + " | ".join(r.get("info", [])[:2]), fingerprint=r.get("fingerprint"))

        # 8. broker
        self.place(c, o, plan)

    def day_open_vs_prev(self, sym):
        """Today's 9:30 open against yesterday's close, in percent (None if unknown)."""
        today = self.ny().date()
        off = (self.ap.get("sim") or {}).get("entry") if self.sim else None
        try:
            ch = entrymod.get_chart(sym, "5m", day=today, offline=os.path.join(ROOT, off) if off else None,
                                    today=today)
        except Exception:
            return None
        prev = ch.get("prev_close")
        first = next((b for b in ch["bars"] if clockmod.to_eastern(datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc).replace(tzinfo=None))
                      .strftime("%Y-%m-%d %H:%M") == today.isoformat() + " 09:30"), None)
        if not prev or not first:
            return None
        return (first["o"] - float(prev)) / float(prev) * 100

    def _sessions(self, sym, rng):
        """(today {slot: bar}, past full sessions) for a symbol, from Yahoo or a sim file."""
        key = (sym, rng, self.ny().strftime("%H%M"))
        cache = self.__dict__.setdefault("_chart_cache", {})
        if key in cache:
            return cache[key]
        off = (self.ap.get("sim") or {}).get("spy" if sym == "SPY" else "tm") if self.sim else None
        if off:
            text = entrymod.read_text(os.path.join(ROOT, off))
        else:
            text = entrymod.yahoo_get(entrymod.chart_path(sym, rng, "5m"))
        res = tmmod.chart_result(text, sym)
        _, bars, _ = tmmod.parse_bars(res, self.now_utc())
        days = tmmod.split_sessions(bars)
        today = self.ny().date().isoformat()
        past, _ = tmmod.full_sessions(days, today)
        out = (days.get(today, {}), [D for _, D in past])
        cache[key] = out
        return out

    def live_features(self, sym, k, n):
        f = {"rvol": None, "market_ok": True, "rs": None}
        try:
            D, past = self._sessions(sym, "1mo")
            f["rvol"] = fx.rvol(D, past, n)
        except Exception as e:
            f["note"] = "stock data: %s" % str(e)[:80]
            D = None
        try:
            spy, _ = self._sessions("SPY", "5d")
            f["market_ok"] = fx.market_ok(spy, k, self.ap.get("market_filter") or "green")
            f["rs"] = fx.rel_strength(D, spy, k) if D else None
            f["spy_change"] = fx.day_change(spy, k)
        except Exception as e:
            f["note"] = (f.get("note", "") + " SPY: %s" % str(e)[:80]).strip()
        for x in ("rvol", "rs", "spy_change"):
            if f.get(x) is not None:
                f[x] = round(f[x], 2)
        return f

    @staticmethod
    def feature_note(f):
        if not f:
            return ""
        bits = []
        if f.get("rvol") is not None:
            bits.append("RVOL %.1fx" % f["rvol"])
        if f.get("spy_change") is not None:
            bits.append("SPY %+.2f%%" % f["spy_change"])
        if f.get("rs") is not None:
            bits.append("vs SPY %+.2f pts" % f["rs"])
        return ("; " + ", ".join(bits)) if bits else ""

    # ------------------------------------------------------------ the broker step
    def approve(self, order, index=1):
        """Record the autopilot's approval in trader/state/broker.json, in the pack's own format,
        after re-checking that trader-risk passed this exact order today."""
        a = argparse.Namespace(orders=os.path.join(T, "orders.json"), config=os.path.join(T, "config.json"),
                               risk=os.path.join(T, "state", "risk.json"),
                               state=os.path.join(T, "state", "broker.json"), now=self.fixed_now, today=None)
        orders, cfg, risk, state, today, now = ticketmod._load(a)
        ready, label, rv = ticketmod.status_of(order, risk, state, today)
        if not ready:
            raise RuntimeError("order not ready: " + label)
        if not (self.ap.get("auto_approve_paper") and cfg.get("paper", True)):
            raise RuntimeError("automatic approval is off, or the account is not paper")
        loss, stop = ticketmod.max_loss(order)
        state.setdefault("orders", []).append({
            "at": ticketmod.ny_iso(now), "date": today, "index": index, "fingerprint": ticketmod.fingerprint(order),
            "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"), "qty": order.get("qty"),
            "kind": ticketmod.kind_of(order, rv), "max_loss": loss, "stop": stop, "plain": ticketmod.plain(order),
            "status": "approved", "reply": AUTO_REPLY, "approved_by": "autopilot", "paper": True,
            "broker": cfg.get("broker", "alpaca")})
        ticketmod.write_json(a.state, state)
        return ticketmod.tool_input(order)

    def place(self, c, order, plan):
        sym = c["symbol"]
        ticket = self.approve(order)
        coid = "ap-%s-%s-%s" % (self.state["date"], sym, self.ny().strftime("%H%M%S"))
        try:
            res = self.broker.submit(ticket, client_order_id=coid)
        except AlpacaError as e:
            self.tool("ticket", "result", "--index", 1, "--status", "failed", "--note", str(e)[:200])
            self.reject(c, "broker", "Alpaca refused: %s" % e)
            self.state["trades"].append({"symbol": sym, "status": "rejected", "note": str(e)[:200],
                                         "at": self.ny().strftime("%H:%M")})
            self.event("error", "order for %s refused by Alpaca: %s" % (sym, e))
            return
        oid = res.get("id")
        filled = self.broker.wait_fill(oid, seconds=4 if self.sim else 20) or res
        fq, fp = filled.get("filled_qty"), filled.get("filled_avg_price")
        args = ["result", "--index", 1, "--status", "placed", "--broker-id", oid]
        if fq and fp and float(fq) > 0:
            args += ["--filled-qty", fq, "--filled-price", fp]
        self.tool("ticket", *args)
        self.tool("alerts", "add", "--from-plan", now=False)
        legs = {l.get("type"): l.get("id") for l in (filled.get("legs") or [])}
        trade = {"symbol": sym, "status": "open" if fq and float(fq) > 0 else "pending", "order_id": oid,
                 "client_order_id": coid, "at": self.ny().strftime("%H:%M"), "qty": float(order["qty"]),
                 "plan": c.get("plan"), "tm": {k: v for k, v in (c.get("tm") or {}).items() if k != "bands"},
                 "max_loss": order["plan"]["max_loss"], "strategy_version": int(self.ap.get("strategy_version", 1)),
                 "exit_mode": self.ap.get("exit_mode", "fixed"), "exit_at_R": float(self.ap.get("exit_at_R", 1.0)),
                 "features": c.get("features"),
                 "fill_price": float(fp) if fp else None,
                 "fill_recorded": bool(fq and float(fq) > 0), "legs": legs, "exit_price": None, "pnl": None}
        self.state["trades"].append(trade)
        c["result"] = "ORDER PLACED"
        self.ok(c, "broker", "bracket buy %s %s sent to Alpaca paper (id %s…), filled %s @ %s" % (
            order["qty"], sym, oid[:8], fq or "-", fp or "-"))
        self.event("trade", "BUY %s %s (paper) entry %.2f stop %.2f target %.2f, filled %s @ %s" % (
            order["qty"], sym, plan["entry"], plan["stop"], plan["target"], fq or "-", fp or "-"),
            order_id=oid)

    # ------------------------------------------------------------ while it is open
    def monitor(self):
        for t in self.state.get("trades", []):
            if t.get("status") not in ("open", "pending"):
                continue
            try:
                o = self.broker.order(t["order_id"])
            except AlpacaError as e:
                self.last_error = str(e)
                continue
            if not o:
                continue
            if o.get("status") in ("canceled", "rejected", "expired") and float(o.get("filled_qty") or 0) == 0:
                t["status"] = "rejected"
                t["note"] = "entry %s" % o.get("status")
                continue
            fq, fp = float(o.get("filled_qty") or 0), o.get("filled_avg_price")
            if fq > 0 and not t.get("fill_recorded"):
                self.tool("ticket", "fill", "--symbol", t["symbol"], "--side", "buy", "--qty", fq, "--price", fp)
                t.update(fill_recorded=True, fill_price=float(fp), status="open", qty=fq)
            legs = o.get("legs") or []
            if t.get("oco_id"):  # after a partial exit the rest is protected by an OCO pair
                oco = self.broker.order(t["oco_id"]) or {}
                legs = [oco] + (oco.get("legs") or [])
            for leg in legs:
                if leg.get("status") == "filled" and leg["id"] not in t.setdefault("legs_recorded", []):
                    q, p = float(leg["filled_qty"]), float(leg["filled_avg_price"])
                    self.tool("ticket", "fill", "--symbol", t["symbol"], "--side", "sell", "--qty", q, "--price", p)
                    t["legs_recorded"].append(leg["id"])
                    self.close_trade(t, p, "target" if leg.get("type") == "limit" else "stop", qty=q)
                    break
            if t.get("status") == "open" and t.get("fill_recorded"):
                self.manage_exit(t, legs)

    def manage_exit(self, t, legs):
        """Breakeven / partial exit once the price is exit_at_R x risk above the fill."""
        mode = t.get("exit_mode") or "fixed"
        if mode == "fixed" or t.get("exit_adjusted"):
            return
        plan = t.get("plan") or {}
        risk, fill = plan.get("risk_per_share"), t.get("fill_price")
        if not risk or not fill:
            return
        trig = fill + float(t.get("exit_at_R", 1.0)) * risk
        px = self.broker.latest_trade(t["symbol"])
        if not px or float(px) < trig:
            return
        be = round(math.ceil(fill * 100) / 100, 2)
        stop_leg = next((l for l in legs if l.get("type") == "stop" and l.get("status") in ("new", "held", "accepted")), None)
        tp_leg = next((l for l in legs if l.get("type") == "limit" and l.get("status") in ("new", "held", "accepted")), None)
        half = int(t["qty"] // 2)
        if mode == "partial" and half >= 1 and tp_leg and stop_leg:
            for l in (tp_leg, stop_leg):
                self.broker.cancel(l["id"])
            time.sleep(0 if self.sim else 1.5)
            try:
                sold = self.broker.submit_raw({"symbol": t["symbol"], "qty": str(half), "side": "sell", "type": "market",
                                               "time_in_force": "day"})
                sold = self.broker.wait_fill(sold["id"], seconds=4 if self.sim else 15) or sold
                rest = t["qty"] - half
                oco = self.broker.submit_raw({"symbol": t["symbol"], "qty": str(int(rest)), "side": "sell", "type": "limit",
                                              "time_in_force": "day", "order_class": "oco",
                                              "take_profit": {"limit_price": "%.2f" % plan["target"]},
                                              "stop_loss": {"stop_price": "%.2f" % be}})
            except AlpacaError as e:
                self.event("error", "%s: partial exit failed (%s); flatten at %s will close it" % (
                    t["symbol"], e, self.cfg.get("flat_by", "15:55")))
                t["exit_adjusted"] = "failed"
                return
            p = float(sold.get("filled_avg_price") or px)
            self.tool("ticket", "fill", "--symbol", t["symbol"], "--side", "sell", "--qty", half, "--price", p)
            t["partials"] = t.get("partials", []) + [{"qty": half, "price": p}]
            t.update(oco_id=oco["id"], exit_adjusted="partial", stop_now=be, qty_open=rest)
            self.event("trade", "%s: +%.1fR reached, sold %d at %.2f, the rest runs to %.2f with the stop at entry %.2f"
                       % (t["symbol"], t.get("exit_at_R", 1.0), half, p, plan["target"], be))
        elif stop_leg:
            try:
                self.broker.replace(stop_leg["id"], stop_price="%.2f" % be)
            except AlpacaError as e:
                self.event("error", "%s: could not move the stop to %.2f: %s" % (t["symbol"], be, e))
                t["exit_adjusted"] = "failed"
                return
            t.update(exit_adjusted="breakeven", stop_now=be)
            self.event("trade", "%s: +%.1fR reached, stop moved to the entry %.2f" % (t["symbol"], t.get("exit_at_R", 1.0), be))

    def close_trade(self, t, price, how, qty=None):
        fill = t.get("fill_price") or price
        parts = t.get("partials", [])
        rest = qty if qty is not None else (t.get("qty_open") or t["qty"])
        t["exit_price"] = price
        t["status"] = how
        t["closed_at"] = self.ny().strftime("%H:%M")
        t["pnl"] = round(sum((p["price"] - fill) * p["qty"] for p in parts) + (price - fill) * rest, 2)
        risk = (t.get("plan") or {}).get("risk_per_share")
        t["R"] = round(t["pnl"] / (risk * t["qty"]), 2) if risk and t["qty"] else None
        self.state["realized"] = round(self.state.get("realized", 0) + t["pnl"], 2)
        self.event("trade", "%s %s closed at %.2f (%s): P&L $%+.2f" % (
            "TARGET" if how == "target" else "STOP" if how == "stop" else "FLAT", t["symbol"], price, how, t["pnl"]))

    # ------------------------------------------------------------ out by the close
    def flatten_all(self):
        mine = {t["symbol"]: t for t in self.state.get("trades", []) if t.get("status") in ("open", "pending")}
        if not mine:
            self.state["flattened"] = True
            return
        for sym, t in mine.items():
            o = self.broker.order(t["order_id"]) or {}
            if o.get("status") in ("new", "accepted", "pending_new", "partially_filled"):
                self.broker.cancel(t["order_id"])
            legs = list(o.get("legs") or [])
            if t.get("oco_id"):
                oco = self.broker.order(t["oco_id"]) or {}
                legs += [oco] + (oco.get("legs") or [])
            for leg in legs:
                if leg.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled"):
                    self.broker.cancel(leg["id"])
            time.sleep(0 if self.sim else 2)
            self.monitor()  # a leg may have filled meanwhile
            if t.get("status") not in ("open", "pending"):
                continue
            pos = self.broker.position(sym)
            qty = float((pos or {}).get("qty") or 0)
            if qty <= 0:
                t["status"] = "rejected" if not t.get("fill_recorded") else t["status"]
                continue
            self.tool("ticket", "flatten", "--symbol", sym, "--qty", qty)
            code, js, err = self.tool_json("risk", timeout=60)
            order = (rj(os.path.join(T, "orders.json"), {}) or {}).get("orders", [{}])[0]
            r = ((js or {}).get("results") or [{}])[0]
            if r.get("verdict") != "PASS":
                self.event("error", "flatten %s: trader-risk said %s %s" % (sym, r.get("verdict"), r.get("reasons")))
                continue
            ticket = self.approve(order)
            try:
                res = self.broker.submit(ticket, client_order_id="ap-flat-%s-%s" % (self.state["date"], sym))
            except AlpacaError as e:
                self.tool("ticket", "result", "--index", 1, "--status", "failed", "--note", str(e)[:200])
                self.event("error", "flatten %s refused: %s" % (sym, e))
                continue
            done = self.broker.wait_fill(res["id"], seconds=4 if self.sim else 20) or res
            fq, fp = done.get("filled_qty"), done.get("filled_avg_price")
            args = ["result", "--index", 1, "--status", "placed", "--broker-id", res["id"]]
            if fq and fp and float(fq) > 0:
                args += ["--filled-qty", fq, "--filled-price", fp]
                self.tool("ticket", *args)
                self.close_trade(t, float(fp), "flat", qty=float(fq))
            else:
                self.tool("ticket", *args)
                t["flat_order_id"] = res["id"]
        self.state["flattened"] = True

    # ------------------------------------------------------------ dashboard file
    def write_status(self, st, phase):
        d = self.state
        cands = sorted(d.get("candidates", {}).values(), key=lambda c: c.get("first_seen", ""), reverse=True)
        open_trades = [t for t in d.get("trades", []) if t.get("status") in ("open", "pending")]
        pos_by = {p["symbol"]: p for p in d.get("positions", [])}
        for t in open_trades:
            p = pos_by.get(t["symbol"])
            if p:
                t["last"] = float(p.get("current_price") or 0)
                t["unrealized"] = float(p.get("unrealized_pl") or 0)
        wj(os.path.join(AP, "status.json"), {
            "updated": self.ny().strftime("%Y-%m-%d %H:%M:%S"), "updated_utc": self.now_utc().isoformat() + "Z",
            "phase": phase, "market": st, "paused": self.paused(), "paper": True,
            "loop_seconds": self.ap["loop_seconds"], "last_error": self.last_error,
            "config": {k: self.cfg.get(k) for k in ("budget", "max_loss_per_trade_usd", "max_daily_loss_usd",
                                                    "max_trades_per_day", "no_entries_before", "no_entries_after",
                                                    "flat_by", "min_reward_risk")},
            "autopilot": {k: v for k, v in self.ap.items() if k not in ("sim",)},
            "account": d.get("account"), "positions": d.get("positions", []),
            "day": {"date": d.get("date"), "symbols": d.get("symbols", []), "funnel": d.get("funnel", {}),
                    "scans": d.get("scans", [])[-30:], "trades": d.get("trades", []), "realized": d.get("realized", 0),
                    "candidates": cands[:60]},
        })


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "tick"):
        p = sub.add_parser(name)
        p.add_argument("--now", help="pin the clock, New York time (replays)")
        p.add_argument("--sim", action="store_true", help="offline tool files from trader/autopilot.json 'sim'")
        p.add_argument("--broker-url", help="mock broker on 127.0.0.1 (tests only)")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    a = ap.parse_args(argv)

    if a.cmd in ("pause", "resume"):
        flag = os.path.join(AP, "PAUSE")
        os.makedirs(AP, exist_ok=True)
        if a.cmd == "pause":
            open(flag, "w").write("paused from the command line\n")
        elif os.path.exists(flag):
            os.remove(flag)
        print("autopilot %s: %s" % ("paused" if a.cmd == "pause" else "resumed",
                                    "no new entries; open trades are still managed" if a.cmd == "pause"
                                    else "new entries allowed"))
        return 0
    if a.cmd == "status":
        s = rj(os.path.join(AP, "status.json"))
        if not s:
            print("no status yet: the autopilot has not run")
            return 1
        acct = s.get("account") or {}
        print("%s  phase %s%s  equity %s  trades today %d  realized $%+.2f" % (
            s["updated"], s["phase"], " (PAUSED)" if s.get("paused") else "", acct.get("equity"),
            len(s["day"]["trades"]), s["day"].get("realized", 0)))
        return 0

    pilot = Pilot(now=a.now, sim=a.sim, broker_url=a.broker_url)
    if a.cmd == "tick":
        pilot.tick()
        return 0
    pilot.event("info", "autopilot started (paper only, loop %ss)" % pilot.ap["loop_seconds"])
    while True:
        pilot.cfg = rj(os.path.join(T, "config.json"), pilot.cfg) or pilot.cfg
        user = rj(os.path.join(T, "autopilot.json"), {}) or {}
        pilot.ap = dict(DEFAULTS, **user)
        pilot.tick()
        time.sleep(max(5, int(pilot.ap["loop_seconds"])))


if __name__ == "__main__":
    sys.exit(main())
