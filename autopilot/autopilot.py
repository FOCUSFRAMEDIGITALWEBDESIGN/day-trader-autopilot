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
import shorts  # noqa: E402
import inplay  # noqa: E402
import notify  # noqa: E402
import events  # noqa: E402
import tunnels  # noqa: E402
scanmod = _mod("ap_scan", TOOLS["scan"])
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
    "short_min_equity": 2000,
    "sides": "both",
    "event_actions": {"fomc": "flat_before", "cpi": "trade", "jobs": "trade"},
    "event_last_entry": "12:00",
    "event_flat_by": "13:55",
    "earnings_action": "skip",
    "setup_b": {"enabled": True, "or_minutes": 5, "last_entry": "10:30", "entries_from": "09:40",
                "only_in_play": True},
    "entry_order": "limit",
    "entry_limit_buffer_R": 0.1,
    "entry_limit_timeout_min": 5,
    "in_play": True,
    "in_play_max": 30,
    "in_play_min_gap": 1.0,
    "in_play_min_rvol": 1.5,
    "in_play_min_price": 10,
    "in_play_universe": 1500,
    "rvol_min": 0,
    "market_filter": "none",
    "rs_min": None,
    "exit_mode": "fixed",
    "exit_at_R": 1.0,
    "exit_atr_k": 2.0,
    "trail_cap_R": 10,
    "learn_years": 2,
    "learn_weekly": True,
    "strategy_version": 1,
    "loop_seconds": 20,
    "scan_delay_seconds": 25,
    "sim": {},
}


# ---------------------------------------------------------------- small helpers

def memory_mb():
    """(total, available) RAM in MB on Linux, (None, None) elsewhere."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) // 1024
        return info.get("MemTotal"), info.get("MemAvailable")
    except (OSError, ValueError):
        return None, None


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
        if not self.sim and not self.fixed_now:
            if level == "trade":
                notify.send(("📈 " if "BUY" in msg or "SHORT" in msg else "🔔 ") + msg)
            elif level == "error":
                notify.send("⚠️ Fehler: " + msg, key="err:" + msg[:60])

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
                # the whole day (every candidate with every stage, features and Time Machine numbers)
                # for the statistics and the weekly export
                full = {k: v for k, v in prev.items() if k not in ("account", "positions") and not k.startswith("_")}
                for c in (full.get("candidates") or {}).values():
                    (c.get("tm") or {}).pop("bands", None)
                wj(os.path.join(AP, "days", "%s.json" % prev["date"]), full)
            self.state = {"date": today, "symbols": [], "premarket_done": False, "last_scan_bar": None,
                          "candidates": {}, "trades": [], "funnel": {}, "scans": [], "realized": 0.0,
                          "flattened": False, "paused": self.state.get("paused", False)}
            self.event("info", "new trading day %s" % today)
            self.state["sim_equity"] = self.simulated_equity()
        return self.state

    def simulated_equity(self):
        """The account as if it held only its start capital: start capital (trader/autopilot/account.json,
        else the budget) plus every realized P&L since the profile was set."""
        import stats
        acc = rj(os.path.join(AP, "account.json"), {}) or {}
        start = float(acc.get("start_capital") or self.cfg.get("budget") or 0)
        since = acc.get("since", "0000-00-00")
        try:
            closed, _, _ = stats.load(AP)
        except Exception:
            closed = []
        today = self.state.get("date")
        pnl = sum(t["pnl"] for t in closed if (t.get("date") or "") >= since and t.get("date") != today)
        return round(start + pnl, 2)

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
                if (d.get("event") or {}).get("action") == "flat_before":
                    flat_m = min(flat_m, hm(self.ap.get("event_flat_by", "13:55")))
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
                        first_or = min(int(self.ap["or_minutes"]), int(self.setup_b().get("or_minutes", 99)))
                        if mins >= hm(st["opens"]) + first_or + 1 and not d.get("in_play_done"):
                            self.stocks_in_play(mins - hm(st["opens"]))
                        self.maybe_scan(local, st)
                else:
                    phase = "after close"
                    self.monitor()
                    if not d.get("summary_sent"):
                        d["summary_sent"] = True
                        self.daily_summary()
        except Exception as e:  # keep the daemon alive; report it
            self.last_error = "%s: %s" % (type(e).__name__, e)
            self.event("error", self.last_error, trace=traceback.format_exc()[-1500:])
        try:
            self.watch_tunnels()
        except Exception as e:
            self.last_error = "tunnel check: %s" % str(e)[:80]
        try:
            self.maybe_learn(phase, mins)
        except Exception as e:
            self.event("error", "learner could not start: %s" % e)
        self.write_status(st, phase)
        self.save()

    def daily_summary(self):
        d = self.state
        closed = [t for t in d.get("trades", []) if t.get("pnl") is not None]
        acct = d.get("account") or {}
        lines = ["📊 Tageszusammenfassung %s" % d.get("date")]
        if closed:
            for t in closed:
                lines.append("%s %s %s: %s, $%+.2f (%+.2f R)" % (
                    "🟢" if t["pnl"] > 0 else "🔴", t["symbol"], "Short" if t.get("side") == "sell" else "Long",
                    {"target": "Ziel", "stop": "Stop", "trail": "Trailing-Stop", "flat": "Flat 15:55"}.get(t["status"], t["status"]),
                    t["pnl"], t.get("R") or 0))
            lines.append("Summe heute: $%+.2f" % d.get("realized", 0))
        else:
            lines.append("Heute keine Trades. Kandidaten geprüft: %d." % len(d.get("candidates", {})))
        if acct.get("equity"):
            lines.append("Paper-Konto: $%s" % acct["equity"])
        notify.send("\n".join(lines))

    # ------------------------------------------------------------ weekly learning
    def watch_tunnels(self, force=False):
        """Every 10 minutes: the public dashboard addresses of this server's quick tunnels. A new
        address (after a restart) goes to Telegram, with your dashboard password for the bot's own."""
        if (self.sim or self.fixed_now) and not force:
            return
        now = time.time()
        if not force and now - getattr(self, "_tunnel_check", 0) < 600:
            return
        self._tunnel_check = now
        hosts = tunnels.hostnames()
        if not hosts:
            return
        ours, others = tunnels.classify(hosts)
        path = os.path.join(AP, "tunnels.json")
        old = rj(path, {}) or {}
        new = {"ours": ours, "others": sorted(others)}
        if new == {"ours": old.get("ours"), "others": old.get("others", [])}:
            return
        wj(path, dict(new, checked=self.ny().strftime("%Y-%m-%d %H:%M")))
        token = os.environ.get("DASHBOARD_TOKEN", "")
        lines = ["🔗 Neue Dashboard-Adressen:"]
        if ours:
            lines.append("Day Trader: https://%s/%s" % (ours, ("?token=" + token) if token else ""))
        for h in others:
            lines.append("Anderer Bot: https://%s" % h)
        self.event("info", "Tunnel-Adresse(n) neu: %s" % ", ".join(([ours] if ours else []) + others))
        notify.send("\n".join(lines))

    def maybe_learn(self, phase, mins):
        """Start autopilot/learn.py after the US close or on a closed day, at most once every 6.5 days,
        at low priority and with a memory cap, so it never slows the trading or a neighbour on the server."""
        if self.sim or self.fixed_now or not self.ap.get("learn_weekly", True):
            return
        proc = getattr(self, "_learn_proc", None)
        if proc is not None:
            code = proc.poll()
            if code is None:
                return
            self._learn_proc = None
            if code != 0:
                self.event("error", "Lernlauf abgebrochen (Code %s), siehe trader/autopilot/learn.log. "
                           "Der aktuelle Plan bleibt unverändert." % code)
        if phase not in ("after close", "closed"):
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
        total, avail = memory_mb()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if avail and avail < 1500:
            env["LEARN_YEARS"] = "1"   # little free memory: one year of history instead of two
        limit = int(min(total * 0.5, 3072) * 1024 * 1024) if total else None

        def lower():   # POSIX only: low CPU priority and a hard memory ceiling for the learner
            try:
                os.nice(15)
                if limit:
                    import resource
                    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except Exception:
                pass
        log = open(os.path.join(AP, "learn.log"), "a", encoding="utf-8")
        self._learn_proc = subprocess.Popen([PY, os.path.join(ROOT, "autopilot", "learn.py")], cwd=ROOT,
                                            stdout=log, stderr=subprocess.STDOUT, env=env,
                                            preexec_fn=lower if os.name != "nt" else None)
        self.event("info", "Lernen gestartet (niedrige Priorität%s%s): neue Daten laden und Strategie prüfen" % (
            ", max. %d MB" % (limit // 1048576) if limit else "",
            ", 1 Jahr Daten (wenig freier Speicher)" if env.get("LEARN_YEARS") else ""))

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
            budget = float(self.state.get("sim_equity") or self.cfg.get("budget") or 0)
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
        if self.ap.get("in_play") and not self.sim:
            liquid = rj(os.path.join(AP, "liquid.json"), {}) or {}
            if not liquid.get("symbols") or liquid.get("built", "") < (datetime.date.today() - datetime.timedelta(days=7)).isoformat():
                try:
                    uni_path = os.path.join(T, "universe.txt")
                    uni = [l.split("|")[0].strip().upper() for l in open(uni_path, encoding="utf-8") if l.strip()]
                    inplay.build_liquid(self.broker, uni, os.path.join(AP, "liquid.json"), int(self.ap["in_play_universe"]),
                                        float(self.ap["in_play_min_price"]), log=lambda m: self.event("info", m))
                except Exception as e:
                    self.event("warn", "liquid universe not built yet: %s" % str(e)[:120])
        blocked = {s.upper() for s in self.cfg.get("blocked", [])}
        d["symbols"] = [s for s in syms if s not in blocked]
        # event day and earnings
        ev = events.today_event(ROOT, d["date"])
        d["event"] = dict(ev, action=events.action_for(ev, self.ap)) if ev else None
        if ev:
            self.event("info", "event day: %s (%s): %s" % (ev.get("name"), ev.get("type"), {
                "skip": "no new entries today", "trade": "trading as usual, trades are tagged",
                "flat_before": "no entries after %s, flat by %s" % (self.ap["event_last_entry"], self.ap["event_flat_by"])}
                .get(d["event"]["action"], d["event"]["action"])))
        d["earnings"] = {}
        try:
            if self.sim:
                off = (self.ap.get("sim") or {}).get("earnings")
                d["earnings"] = events.parse_nasdaq(rj(os.path.join(ROOT, off), {})) if off else {}
            else:
                d["earnings"] = events.earnings_today(d["date"])
            if d["earnings"]:
                self.event("info", "earnings today: %d companies report (Nasdaq calendar)" % len(d["earnings"]))
        except Exception as e:
            self.event("warn", "earnings calendar not available today: %s" % str(e)[:120])
        d["premarket_done"] = True
        self.event("info", "today's scan list: %d symbols" % len(d["symbols"]))

    # ------------------------------------------------------------ stocks in play
    def stocks_in_play(self, minutes_open):
        """Once a day, when the opening range is complete: add the stocks in play to the scan list."""
        d = self.state
        d["in_play_done"] = True
        if not self.ap.get("in_play") or (self.sim and not self.ap.get("sim_in_play")):
            return
        path = os.path.join(AP, "liquid.json")
        liquid = rj(path, {}) or {}
        try:
            if not liquid.get("symbols") or liquid.get("built", "") < (datetime.date.today() - datetime.timedelta(days=7)).isoformat():
                uni_path = os.path.join(T, "universe.txt")
                if not os.path.exists(uni_path):
                    self.tool("scan", "universe", now=False)
                uni = [l.split("|")[0].strip().upper() for l in open(uni_path, encoding="utf-8") if l.strip()]
                self.event("info", "stocks in play: building the liquid universe from %d stocks (weekly, a few minutes)" % len(uni))
                liquid = inplay.build_liquid(self.broker, uni, path, int(self.ap["in_play_universe"]),
                                             float(self.ap["in_play_min_price"]), log=lambda m: self.event("info", m))
            got = inplay.rank(self.broker, liquid.get("symbols", []), minutes_open, float(self.ap["in_play_min_gap"]),
                              float(self.ap["in_play_min_rvol"]), int(self.ap["in_play_max"]))
        except Exception as e:
            self.event("warn", "stocks in play skipped: %s" % str(e)[:160])
            return
        blocked = {s.upper() for s in self.cfg.get("blocked", [])}
        new = [r["symbol"] for r in got if r["symbol"] not in d["symbols"] and r["symbol"] not in blocked]
        d["symbols"] += new
        d["in_play"] = got
        self.event("info", "stocks in play: %d found, %d new on today's list%s" % (
            len(got), len(new), (": " + ", ".join("%s %+.1f%% %.1fx" % (r["symbol"], r["gap"], r["rvol"]) for r in got[:8]))
            if got else ""))

    # ------------------------------------------------------------ every 5-minute bar
    def maybe_scan(self, local, st):
        d = self.state
        mins = local.hour * 60 + local.minute
        bar = (mins - 570) // 5
        sec_into = (mins - 570) % 5 * 60 + local.second
        if d.get("last_scan_bar") == bar or (sec_into < int(self.ap["scan_delay_seconds"]) and not self.fixed_now):
            return
        d["last_scan_bar"] = bar
        first_or = min(int(self.ap["or_minutes"]), int(self.setup_b().get("or_minutes", 99)))
        if mins < 570 + first_or + 5:
            return  # every range is still forming
        ev = d.get("event") or {}
        if ev.get("action") == "skip":
            return
        if ev.get("action") == "flat_before" and mins > hm(self.ap.get("event_last_entry", "12:00")):
            return
        if mins > max(hm(self.ap.get("last_entry") or self.cfg.get("no_entries_after", "15:30")),
                      hm(self.setup_b().get("last_entry", "00:00")) if self.setup_b() else 0):
            return
        if self.paused():
            return
        lim = self.ap.get("stop_after_daily_loss_usd")
        if lim and d.get("realized", 0) <= -abs(float(lim)):
            return
        open_n = sum(1 for t in d["trades"] if t.get("status") in ("open", "pending"))
        if open_n >= int(self.ap["max_open_positions"]):
            return
        entries = sum(1 for t in d["trades"] if t.get("status") not in ("rejected", "missed"))
        if entries >= int(self.cfg.get("max_trades_per_day", 3)):
            return
        self.scan_and_trade(mins)

    def setup_b(self):
        """The second setup (5-minute opening range on stocks in play), {} when it is off."""
        b = self.ap.get("setup_b") or {}
        return b if b.get("enabled") else {}

    def scan_and_trade(self, mins):
        d = self.state
        setups = []
        if mins >= 570 + int(self.ap["or_minutes"]) + 5 and \
                mins <= hm(self.ap.get("last_entry") or self.cfg.get("no_entries_after", "15:30")):
            setups.append({"name": "ORB %d min" % int(self.ap["or_minutes"]), "suffix": "",
                           "or_minutes": int(self.ap["or_minutes"]), "symbols": d["symbols"]})
        b = self.setup_b()
        if b:
            syms = [r["symbol"] for r in d.get("in_play") or []] if b.get("only_in_play", True) else d["symbols"]
            if syms and 570 + int(b["or_minutes"]) + 5 <= mins <= hm(b.get("last_entry", "10:30")):
                setups.append({"name": "ORB %d min (in play)" % int(b["or_minutes"]), "suffix": ":B",
                               "or_minutes": int(b["or_minutes"]), "symbols": syms, "entries_from": b.get("entries_from")})
        for su in setups:
            self.scan_setup(mins, su)

    def scan_setup(self, mins, su):
        d = self.state
        if not su["symbols"]:
            return
        sides = self.ap.get("sides", "long")
        hits, scanned, note = [], None, None
        if sides in ("long", "both"):
            args = ["orb", "--symbols", ",".join(su["symbols"]), "--or-minutes", su["or_minutes"],
                    "--min-price", self.ap["scan_min_price"], "--show", 200]
            code, js, err = self.tool_json("scan", *args, timeout=400)
            if js:
                hits += [dict(h, side="buy") for h in js.get("hits", [])]
                scanned = js.get("scanned")
                if not js.get("scanned") and js.get("failed"):
                    note = "Yahoo: %s of %s charts failed" % (js.get("failed"), js.get("universe") or "?")
            else:
                note = err
        eq = float(d.get("sim_equity") or 0) + float(d.get("realized") or 0)
        if sides in ("short", "both") and eq and eq < float(self.ap.get("short_min_equity", 2000)):
            if not d.get("short_off_noted"):
                d["short_off_noted"] = True
                self.event("info", "short selling off today: the simulated account ($%.2f) is under $%.0f, "
                           "Alpaca's minimum for shorting" % (eq, float(self.ap.get("short_min_equity", 2000))))
            sides = "long" if sides == "both" else "none"
        if sides in ("short", "both"):
            try:
                local = self.ny()
                off = (self.ap.get("sim") or {}).get("scan") if self.sim else None
                sh, n_sc = shorts.scan_breakdowns(scanmod, su["symbols"], local.date(), local.replace(second=0),
                                                  su["or_minutes"] // 5, float(self.ap["scan_min_price"]),
                                                  offline=os.path.join(ROOT, off) if off else None)
                hits += [dict(h, side="sell") for h in sh]
                scanned = max(scanned or 0, n_sc)
                if not n_sc and su["symbols"] and not note:
                    note = "short scan: no usable chart for %d symbols" % len(su["symbols"])
            except Exception as e:
                note = "short scan: %s" % str(e)[:120]
        d["scans"].append({"t": self.ny().strftime("%H:%M"), "setup": su["name"], "scanned": scanned, "hits": len(hits),
                           "long": sum(1 for h in hits if h["side"] == "buy"),
                           "short": sum(1 for h in hits if h["side"] == "sell"), "note": note})
        d["scans"] = d["scans"][-100:]
        # the data source: three failed scans in a row means Yahoo is blocking or down
        if note and not hits:
            d["data_fail"] = d.get("data_fail", 0) + 1
            if d["data_fail"] == 3:
                self.last_error = "Kursdaten: 3 Scans hintereinander ohne Daten (%s)" % str(note)[:120]
                self.event("error", "Kursdaten (Yahoo) liefern seit 3 Scans nichts: %s. Der Bot findet so keine "
                           "Setups; offene Trades sichert Alpaca weiter." % str(note)[:160])
        else:
            if d.get("data_fail", 0) >= 3:
                self.event("info", "Kursdaten sind wieder da.")
                self.last_error = None
            d["data_fail"] = 0
        fresh = []
        for h in hits:
            sym = h["symbol"]
            key = (sym if h["side"] == "buy" else sym + ":short") + su["suffix"]
            if key in d["candidates"]:
                continue
            age = mins - hm(h["at"])
            c = {"symbol": sym, "side": h["side"], "first_seen": self.ny().strftime("%H:%M"), "scan": h, "stages": {},
                 "setup": su["name"], "or_minutes": su["or_minutes"], "entries_from": su.get("entries_from")}
            d["candidates"][key] = c
            self.bump("scanner")
            if age > int(self.ap["max_signal_age_min"]):
                self.reject(c, "scanner", "broke out at %s, %d min ago: a late entry is a different trade"
                            % (h["at"], age))
                continue
            if self.ap.get("max_pct_over") and h.get("pct_over", 0) > float(self.ap["max_pct_over"]):
                self.reject(c, "scanner", "closed %.2f%% %s the range, more than %.2f%%: extended"
                            % (h["pct_over"], "over" if h["side"] == "buy" else "under", float(self.ap["max_pct_over"])))
                continue
            c["stages"]["scanner"] = {"ok": True, "note": "closed %.2f%% %s the range at %s" % (
                h["pct_over"], "over" if h["side"] == "buy" else "under", h["at"])}
            fresh.append(c)
        if fresh:
            self.event("info", "scan %s (%s): %d signals, %d fresh: %s" % (self.ny().strftime("%H:%M"), su["name"], len(hits),
                       len(fresh), ", ".join(c["symbol"] + ("" if c["side"] == "buy" else " (short)") for c in fresh)))
        for c in fresh[: int(self.ap["max_candidates_per_bar"])]:
            open_n = sum(1 for t in d["trades"] if t.get("status") in ("open", "pending"))
            entries = sum(1 for t in d["trades"] if t.get("status") not in ("rejected", "missed"))
            if open_n >= int(self.ap["max_open_positions"]) or entries >= int(self.cfg.get("max_trades_per_day", 3)):
                self.reject(c, "limits", "the day's position / trade limit is reached")
                continue
            if any(t.get("symbol") == c["symbol"] and t.get("status") in ("open", "pending") for t in d["trades"]):
                self.reject(c, "limits", "a trade in %s is already open" % c["symbol"])
                continue
            (self.pipeline if c["side"] == "buy" else self.pipeline_short)(c)
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
    def step_news(self, c):
        sym = c["symbol"]
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
            self.reject(c, "news", "blocked by news: %s" % bad[0].get("title", "")[:120])
            return False
        self.ok(c, "news", "%d items in %sh, none blocking%s" % (
            len(items), self.ap["news_hours"], ("; %+.1f%% vs yesterday" % move["pct"]) if move and
            move.get("pct") is not None else ""))
        rep = (self.state.get("earnings") or {}).get(sym)
        if rep:
            c["earnings"] = rep
            if self.ap.get("earnings_action", "skip") == "skip":
                self.reject(c, "news", "earnings today (%s): skipped" % {"pre": "before the open", "after": "after the close"}.get(rep, "time unknown"))
                return False
        if self.state.get("event"):
            c["event"] = self.state["event"].get("name")
        return True

    def pipeline(self, c):
        sym = c["symbol"]
        # 2. news
        if not self.step_news(c):
            return

        # 3. entry
        eargs = ["check", sym, "--write", "--or-minutes", c.get("or_minutes") or self.ap["or_minutes"],
                 "--config", self.rules_for(c)]
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
            f = self.live_features(sym, int(trig["bar_index"]), int(c.get("or_minutes") or self.ap["or_minutes"]) // 5)
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
        if not self.step_tm(c, plan):
            return
        # fresh price for budget and risk (the higher price means a bigger risk, fewer shares)
        ok, last = self.step_price(c, plan, "buy")
        if not ok:
            return
        return self.long_budget_risk_place(c, plan)

    def step_tm(self, c, plan):
        code, js, err = self.tool_json("tm", "run", "--write", "--until", self.cfg.get("flat_by", "15:55"),
                                       "--replays", self.ap["tm_replays"], "--seed", self.ap["tm_seed"], timeout=300)
        if not js or not js.get("mc"):
            self.reject(c, "timemachine", "time machine could not run: %s" % err)
            return False
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
            self.reject(c, "timemachine", why)
            return False
        self.ok(c, "timemachine", "target %.1f%% / stop %.1f%% / neither %.1f%%, edge %+.1f pts, avg R %+.3f" % (
            100 * mc["p_target"], 100 * mc["p_stop"], 100 * mc["p_neither"], edge, mc["avg_R"]))
        return True

    def step_price(self, c, plan, side):
        """A fresh price (Alpaca real time, Yahoo as the fallback) into trader/prices.json.
        Returns (still_valid, price)."""
        sym = c["symbol"]
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
        past_t = last and (last >= plan["target"] if side == "buy" else last <= plan["target"])
        past_s = last and (last <= plan["stop"] if side == "buy" else last >= plan["stop"])
        if past_t or past_s:
            self.reject(c, "budget", "price %.2f is already past the %s" % (last, "target" if past_t else "stop"))
            return False, last
        return True, last

    def long_budget_risk_place(self, c, plan):
        sym = c["symbol"]
        # 6. budget (a stale orders.json must never be mistaken for this one)
        try:
            os.remove(os.path.join(T, "orders.json"))
        except OSError:
            pass
        size_args = ["--write"]
        lim = self.entry_limit(plan, "buy")
        if lim:
            size_args += ["--limit", "%.2f" % lim]
        code, out, err = self.tool("size", *size_args, timeout=60)
        orders = rj(os.path.join(T, "orders.json"), {}) or {}
        if code != 0 or not orders.get("orders") or (orders.get("source", "").find(sym) < 0):
            text = (out or "") + "\n" + (err or "")
            why = next((l.strip() for l in text.splitlines() if l.strip().lower().startswith(("no order", "refused"))), None) \
                or next((l.strip() for l in reversed(text.splitlines()) if l.strip() and "financial advice" not in l), "?")
            return self.reject(c, "budget", "budget refused: %s" % why[:200])
        o = orders["orders"][0]
        self.ok(c, "budget", "%s shares, max loss $%.2f" % (o["qty"], o["plan"]["max_loss"]),
                qty=o["qty"], max_loss=o["plan"]["max_loss"])

        # 7. risk
        code, js, err = self.tool_json("risk", "--config", self.rules_for(c), timeout=60)
        r = ((js or {}).get("results") or [{}])[0]
        if r.get("verdict") != "PASS":
            return self.reject(c, "risk", "BLOCK: %s" % "; ".join(r.get("reasons") or [str(err)]))
        self.ok(c, "risk", "PASS: " + " | ".join(r.get("info", [])[:2]), fingerprint=r.get("fingerprint"))

        # 8. broker
        self.place(c, o, plan)

    def pipeline_short(self, c):
        """An opening-range breakdown: the same chain, mirrored (autopilot/shorts.py)."""
        sym, h = c["symbol"], c["scan"]
        if not self.step_news(c):
            return
        # 3. entry: the breakdown bar from the scan, checked like trader-entry checks a breakout
        n = int(c.get("or_minutes") or self.ap["or_minutes"]) // 5
        if self.ap.get("gap_filter"):
            gap = self.day_open_vs_prev(sym)
            if gap is not None and gap > 0:
                return self.reject(c, "entry", "opened %.2f%% above yesterday's close: gap filter (short)" % gap)
        if self.ap.get("rvol_min") or self.ap.get("market_filter") not in (None, "none") \
                or self.ap.get("rs_min") is not None:
            f = self.live_features(sym, int(h["bar_index"]), n, side="sell")
            c["features"] = f
            ok, why = fx.passes(f, self.ap)
            if not ok:
                return self.reject(c, "entry", "filter: " + why)
        trig = {"time": h["at"], "price": h["price"], "bar_index": h["bar_index"], "pct_over": -h["pct_over"]}
        self.ok(c, "entry", "breakdown %s at %.2f (%.2f%% under %.2f)%s" % (
            h["at"], h["price"], h["pct_over"], h["or_low"], self.feature_note(c.get("features"))),
            trigger=trig, opening_range={"high": h["or_high"], "low": h["or_low"], "minutes": n * 5})
        # 4. exit plan (mirror of trader-exit) -> trader/plan.json for the Time Machine
        plan, why = shorts.plan_short(sym, self.state["date"], h["price"], h["at"], h["bar_index"], h["or_high"],
                                      h["or_low"], self.ap["stop"], self.ap["target"], self.cfg.get("flat_by", "15:55"),
                                      n * 5)
        if not plan:
            return self.reject(c, "exit", "short plan refused: " + why)
        risk_pct = plan["risk_per_share"] / plan["entry"] * 100
        if not (float(self.ap["min_risk_pct"]) <= risk_pct <= float(self.ap["max_risk_pct"])):
            return self.reject(c, "exit", "risk %.2f%% of the price is outside %.2f-%.2f%%" % (
                risk_pct, float(self.ap["min_risk_pct"]), float(self.ap["max_risk_pct"])))
        wj(os.path.join(T, "plan.json"), plan)
        c["plan"] = {k: plan.get(k) for k in ("entry", "stop", "target", "risk_per_share", "reward_risk",
                                              "target_r", "stop_label", "flat_by", "entry_time")}
        self.ok(c, "exit", "short: stop %.2f, target %.2f (%sR), risk %.2f a share" % (
            plan["stop"], plan["target"], plan["target_r"], plan["risk_per_share"]))
        # 5. time machine (the pack's, side sell)
        if not self.step_tm(c, plan):
            return
        ok, last = self.step_price(c, plan, "sell")
        if not ok:
            return
        # can it be shorted at all? (Alpaca: shortable and easy to borrow)
        try:
            asset = self.broker.get("/v2/assets/" + sym)
        except AlpacaError:
            asset = None
        if asset and not (asset.get("shortable") and asset.get("easy_to_borrow")):
            return self.reject(c, "budget", "Alpaca cannot short %s (not shortable / hard to borrow)" % sym)
        # 6. budget (mirror of trader-budget)
        pf = rj(os.path.join(T, "portfolio.json"), {}) or {}
        order, why = shorts.size_short(self.cfg, plan, price=last, cash=pf.get("cash"),
                                       limit=self.entry_limit(plan, "sell"))
        if not order:
            return self.reject(c, "budget", "short size refused: " + why)
        wj(os.path.join(T, "orders.json"), shorts.orders_doc(order, self.now_utc()))
        self.ok(c, "budget", "short %s shares, max loss $%.2f" % (order["qty"], order["plan"]["max_loss"]),
                qty=order["qty"], max_loss=order["plan"]["max_loss"])
        # 7. risk: the pack's own check, with allow_short on for this order only
        code, js, err = self.tool_json("risk", "--config", self.rules_for(c), timeout=60)
        r = ((js or {}).get("results") or [{}])[0]
        if r.get("verdict") != "PASS":
            return self.reject(c, "risk", "BLOCK: %s" % "; ".join(r.get("reasons") or [str(err)]))
        self.ok(c, "risk", "PASS: " + " | ".join(r.get("info", [])[:2]), fingerprint=r.get("fingerprint"))
        # 8. broker
        self.place(c, order, plan)

    def rules_for(self, c):
        """The pack's rules (trader/config.json) as they apply to this candidate: unchanged for a
        15-minute long; with allow_short on for a short; with the earlier entry window of setup B.
        Written to trader/autopilot/rules-*.json, the user's config is never changed."""
        cfg = dict(self.cfg)
        tag = []
        if c.get("side") == "sell":
            cfg["allow_short"] = True
            tag.append("short")
        if c.get("entries_from"):
            cfg["no_entries_before"] = c["entries_from"]
            tag.append("b")
        if not tag:
            return os.path.join(T, "config.json")
        path = os.path.join(AP, "rules-%s.json" % "-".join(tag))
        wj(path, cfg)
        return path

    def entry_limit(self, plan, side):
        """A limit entry a little past the signal price (entry_limit_buffer_R x risk), or None for market."""
        if self.ap.get("entry_order", "limit") != "limit":
            return None
        buf = float(self.ap.get("entry_limit_buffer_R", 0.1)) * plan["risk_per_share"]
        return round(plan["entry"] + (buf if side == "buy" else -buf), 2)

    def record_fill(self, t, side, qty, price):
        """A fill in the pack's books: trader-broker's ticket.py for longs; for shorts the
        portfolio is kept by shorts.apply_fill (the pack cannot hold a negative position)."""
        if t.get("side", "buy") == "buy":
            self.tool("ticket", "fill", "--symbol", t["symbol"], "--side", side, "--qty", qty, "--price", price)
            return
        shorts.apply_fill(os.path.join(T, "portfolio.json"), self.cfg, t["symbol"], side, qty, price, self.state["date"])
        ticketmod.log_fill(os.path.join(T, "state", "broker.json"), self.now_utc(), self.state["date"],
                           t["symbol"], side, qty, price)

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

    def live_features(self, sym, k, n, side="buy"):
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
            if side == "sell":   # oriented for a short: SPY red is ok, weaker than SPY is strong
                f["market_ok"] = f["spy_change"] is None or f["spy_change"] < 0
                f["rs"] = None if f["rs"] is None else -f["rs"]
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
        is_limit = order.get("type") == "limit"
        filled = self.broker.wait_fill(oid, seconds=2 if self.sim else (8 if is_limit else 20)) or res
        fq, fp = filled.get("filled_qty"), filled.get("filled_avg_price")
        side = order.get("side", "buy")
        args = ["result", "--index", 1, "--status", "placed", "--broker-id", oid]
        if fq and fp and float(fq) > 0 and side == "buy":
            args += ["--filled-qty", fq, "--filled-price", fp]
        self.tool("ticket", *args)
        if side == "sell" and fq and fp and float(fq) > 0:
            self.record_fill({"symbol": sym, "side": "sell"}, "sell", fq, fp)
        if side == "buy":
            self.tool("alerts", "add", "--from-plan", now=False)
        legs = {l.get("type"): l.get("id") for l in (filled.get("legs") or [])}
        trade = {"symbol": sym, "side": side, "status": "open" if fq and float(fq) > 0 else "pending", "order_id": oid,
                 "client_order_id": coid, "at": self.ny().strftime("%H:%M"), "qty": float(order["qty"]),
                 "plan": c.get("plan"), "tm": {k: v for k, v in (c.get("tm") or {}).items() if k != "bands"},
                 "max_loss": order["plan"]["max_loss"], "strategy_version": int(self.ap.get("strategy_version", 1)),
                 "exit_mode": self.ap.get("exit_mode", "fixed"), "exit_at_R": float(self.ap.get("exit_at_R", 1.0)),
                 "features": c.get("features"),
                 "fill_price": float(fp) if fp else None,
                 "fill_recorded": bool(fq and float(fq) > 0), "legs": legs, "exit_price": None, "pnl": None}
        trade["date"] = self.state["date"]
        trade["entry_order"] = order.get("type", "market")
        trade["setup"] = c.get("setup")
        trade["event"] = (self.state.get("event") or {}).get("name")
        trade["earnings"] = c.get("earnings")
        trade["limit_price"] = float(order["limit_price"]) if order.get("limit_price") else None
        trade["signal_price"] = plan["entry"]
        trade["in_play"] = sym in {r.get("symbol") for r in self.state.get("in_play") or []}
        trade["stop_log"] = [[trade["at"], plan["stop"]]]
        trade["or"] = (c["stages"].get("entry") or {}).get("opening_range")
        trade["entry_bar"] = int(((c["stages"].get("entry") or {}).get("trigger") or {}).get("bar_index", 0))
        if trade["exit_mode"] in fx.TRAILING:
            self.start_trailing(trade)
        self.state["trades"].append(trade)
        c["result"] = "ORDER PLACED"
        self.ok(c, "broker", "bracket %s %s %s sent to Alpaca paper (id %s…), filled %s @ %s" % (
            "buy" if side == "buy" else "short", order["qty"], sym, oid[:8], fq or "-", fp or "-"))
        self.event("trade", "%s %s %s (paper) entry %.2f stop %.2f target %.2f, filled %s @ %s" % (
            "BUY" if side == "buy" else "SHORT", order["qty"], sym, plan["entry"], plan["stop"], plan["target"],
            fq or "-", fp or "-"),
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
            if t.get("status") == "pending" and o.get("status") in ("new", "accepted", "pending_new", "partially_filled"):
                waited = hm(self.ny().strftime("%H:%M")) - hm(t.get("at", "00:00"))
                if waited >= int(self.ap.get("entry_limit_timeout_min", 5)):
                    self.broker.cancel(t["order_id"])
                    o = self.broker.order(t["order_id"]) or o
                    if float(o.get("filled_qty") or 0) == 0:
                        t["status"] = "missed"
                        t["note"] = "limit %.2f not filled within %d min" % (t.get("limit_price") or 0, waited)
                        self.event("info", "%s: limit entry %.2f not filled in %d min, canceled (missed)" % (
                            t["symbol"], t.get("limit_price") or 0, waited))
                        continue
            if o.get("status") in ("canceled", "rejected", "expired") and float(o.get("filled_qty") or 0) == 0:
                t["status"] = "rejected"
                t["note"] = "entry %s" % o.get("status")
                continue
            fq, fp = float(o.get("filled_qty") or 0), o.get("filled_avg_price")
            if fq > 0 and not t.get("fill_recorded"):
                self.record_fill(t, t.get("side", "buy"), fq, fp)
                t.update(fill_recorded=True, fill_price=float(fp), status="open", qty=fq)
                if t.get("exit_mode") in fx.TRAILING and not t.get("target_cap"):
                    self.start_trailing(t)
            legs = o.get("legs") or []
            if t.get("oco_id"):  # after a partial exit the rest is protected by an OCO pair
                oco = self.broker.order(t["oco_id"]) or {}
                legs = [oco] + (oco.get("legs") or [])
            for leg in legs:
                if leg.get("status") == "filled" and leg["id"] not in t.setdefault("legs_recorded", []):
                    q, p = float(leg["filled_qty"]), float(leg["filled_avg_price"])
                    self.record_fill(t, "sell" if t.get("side", "buy") == "buy" else "buy", q, p)
                    t["legs_recorded"].append(leg["id"])
                    sgn = 1 if t.get("side", "buy") == "buy" else -1
                    how = "target" if leg.get("type") == "limit" else \
                        ("trail" if t.get("exit_mode") in fx.TRAILING and sgn * (p - (t.get("fill_price") or p)) > 0
                         else "stop")
                    self.close_trade(t, p, how, qty=q)
                    break
            if t.get("status") == "open" and t.get("fill_recorded"):
                self.manage_exit(t, legs)

    def start_trailing(self, t):
        """Trailing exits have no fixed target: the bracket's take-profit is moved far away
        (trail_cap_R x risk) and the ATR is measured on today's bars up to the entry bar."""
        plan = t.get("plan") or {}
        t["exit_atr_k"] = float(self.ap.get("exit_atr_k", 2.0))
        try:
            D, _ = self._sessions(t["symbol"], "1d")
            t["atr"] = round(fx.atr([D[i] for i in sorted(D) if i <= t["entry_bar"]]) or 0, 4) or None
        except Exception as e:
            t["atr"] = None
            self.event("warn", "%s: no ATR (%s)" % (t["symbol"], str(e)[:80]))
        if not t.get("fill_price") or not plan.get("risk_per_share"):
            return
        sgn = 1 if t.get("side", "buy") == "buy" else -1
        cap = round(t["fill_price"] + sgn * float(self.ap.get("trail_cap_R", 10)) * plan["risk_per_share"], 2)
        if cap <= 0:
            cap = 0.01
        tp = t.get("legs", {}).get("limit")
        try:
            if tp:
                self.broker.replace(tp, limit_price="%.2f" % cap)
                t["target_cap"] = cap
        except AlpacaError as e:
            self.event("error", "%s: could not lift the take-profit for trailing: %s" % (t["symbol"], e))

    def trail(self, t, legs):
        """Once per completed 5-minute bar: move the stop up to fx.trail_level, never down."""
        bar_key = self.ny().strftime("%H") + str((self.ny().minute // 5) * 5)
        if t.get("trail_bar") == bar_key:
            return
        t["trail_bar"] = bar_key
        plan = t.get("plan") or {}
        try:
            D, _ = self._sessions(t["symbol"], "1d")
        except Exception as e:
            self.last_error = "trailing %s: %s" % (t["symbol"], str(e)[:80])
            return
        after = [D[i] for i in sorted(D) if i > t.get("entry_bar", 0)]
        if not after:
            return
        side = t.get("side", "buy")
        sgn = 1 if side == "buy" else -1
        high = max([t["fill_price"]] + [b["h"] for b in after]) if side == "buy" else \
            min([t["fill_price"]] + [b["l"] for b in after])
        cur = t.get("stop_now") or plan["stop"]
        new = fx.trail_level_side(side, t["exit_mode"], t["fill_price"], cur, plan["risk_per_share"], high,
                                  t.get("atr"), float(t.get("exit_atr_k", 2.0)))
        last = after[-1]["c"]
        if sgn * (new - cur) <= 0.004 or sgn * (new - last) >= 0:
            return   # nothing to do, or the stop would sit at/through the market (it would fill at once)
        stop_leg = next((l for l in legs if l.get("type") == "stop" and l.get("status") in ("new", "held", "accepted")), None)
        if not stop_leg:
            return
        try:
            self.broker.replace(stop_leg["id"], stop_price="%.2f" % new)
        except AlpacaError as e:
            self.event("error", "%s: could not trail the stop to %.2f: %s" % (t["symbol"], new, e))
            return
        t["stop_now"] = new
        t.setdefault("trail_log", []).append([self.ny().strftime("%H:%M"), new])
        t.setdefault("stop_log", []).append([self.ny().strftime("%H:%M"), new])
        self.event("trade", "%s: trailing stop %.2f -> %.2f (%s %.2f, %+.2fR locked)" % (
            t["symbol"], cur, new, "high" if side == "buy" else "low", high,
            sgn * (new - t["fill_price"]) / plan["risk_per_share"]))

    def manage_exit(self, t, legs):
        """Breakeven / partial exit once the price is exit_at_R x risk above the fill;
        trailing stops once per bar."""
        mode = t.get("exit_mode") or "fixed"
        if mode in fx.TRAILING:
            return self.trail(t, legs)
        if mode == "fixed" or t.get("exit_adjusted"):
            return
        plan = t.get("plan") or {}
        risk, fill = plan.get("risk_per_share"), t.get("fill_price")
        if not risk or not fill:
            return
        side = t.get("side", "buy")
        sgn = 1 if side == "buy" else -1
        trig = fill + sgn * float(t.get("exit_at_R", 1.0)) * risk
        px = self.broker.latest_trade(t["symbol"])
        if not px or sgn * (float(px) - trig) < 0:
            return
        be = round((math.ceil(fill * 100) if side == "buy" else math.floor(fill * 100)) / 100, 2)
        out_side = "sell" if side == "buy" else "buy"
        stop_leg = next((l for l in legs if l.get("type") == "stop" and l.get("status") in ("new", "held", "accepted")), None)
        tp_leg = next((l for l in legs if l.get("type") == "limit" and l.get("status") in ("new", "held", "accepted")), None)
        half = int(t["qty"] // 2)
        if mode == "partial" and half >= 1 and tp_leg and stop_leg:
            for l in (tp_leg, stop_leg):
                self.broker.cancel(l["id"])
            time.sleep(0 if self.sim else 1.5)
            try:
                sold = self.broker.submit_raw({"symbol": t["symbol"], "qty": str(half), "side": out_side,
                                               "type": "market", "time_in_force": "day"}, closing_short=side == "sell")
                sold = self.broker.wait_fill(sold["id"], seconds=4 if self.sim else 15) or sold
                rest = t["qty"] - half
                oco = self.broker.submit_raw({"symbol": t["symbol"], "qty": str(int(rest)), "side": out_side,
                                              "type": "limit", "time_in_force": "day", "order_class": "oco",
                                              "take_profit": {"limit_price": "%.2f" % plan["target"]},
                                              "stop_loss": {"stop_price": "%.2f" % be}}, closing_short=side == "sell")
            except AlpacaError as e:
                self.event("error", "%s: partial exit failed (%s); flatten at %s will close it" % (
                    t["symbol"], e, self.cfg.get("flat_by", "15:55")))
                t["exit_adjusted"] = "failed"
                return
            p = float(sold.get("filled_avg_price") or px)
            self.record_fill(t, out_side, half, p)
            t["partials"] = t.get("partials", []) + [{"qty": half, "price": p, "at": self.ny().strftime("%H:%M")}]
            t.setdefault("stop_log", []).append([self.ny().strftime("%H:%M"), be])
            t.update(oco_id=oco["id"], exit_adjusted="partial", stop_now=be, qty_open=rest)
            self.event("trade", "%s: +%.1fR reached, %s %d at %.2f, the rest runs to %.2f with the stop at entry %.2f"
                       % (t["symbol"], t.get("exit_at_R", 1.0), "sold" if side == "buy" else "covered", half, p,
                          plan["target"], be))
        elif stop_leg:
            try:
                self.broker.replace(stop_leg["id"], stop_price="%.2f" % be)
            except AlpacaError as e:
                self.event("error", "%s: could not move the stop to %.2f: %s" % (t["symbol"], be, e))
                t["exit_adjusted"] = "failed"
                return
            t.update(exit_adjusted="breakeven", stop_now=be)
            t.setdefault("stop_log", []).append([self.ny().strftime("%H:%M"), be])
            self.event("trade", "%s: +%.1fR reached, stop moved to the entry %.2f" % (t["symbol"], t.get("exit_at_R", 1.0), be))

    def close_trade(self, t, price, how, qty=None):
        fill = t.get("fill_price") or price
        parts = t.get("partials", [])
        rest = qty if qty is not None else (t.get("qty_open") or t["qty"])
        t["exit_price"] = price
        t["status"] = how
        t["closed_at"] = self.ny().strftime("%H:%M")
        sgn = 1 if t.get("side", "buy") == "buy" else -1
        t["pnl"] = round(sgn * (sum((p["price"] - fill) * p["qty"] for p in parts) + (price - fill) * rest), 2)
        risk = (t.get("plan") or {}).get("risk_per_share")
        t["R"] = round(t["pnl"] / (risk * t["qty"]), 2) if risk and t["qty"] else None
        self.state["realized"] = round(self.state.get("realized", 0) + t["pnl"], 2)
        self.event("trade", "%s %s%s closed at %.2f (%s): P&L $%+.2f" % (
            {"target": "TARGET", "stop": "STOP", "trail": "TRAILING STOP"}.get(how, "FLAT"), t["symbol"],
            "" if sgn > 0 else " (short)", price, how, t["pnl"]))

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
            short = t.get("side", "buy") == "sell"
            if (qty >= 0) if short else (qty <= 0):
                t["status"] = "rejected" if not t.get("fill_recorded") else t["status"]
                continue
            if short:   # buy to cover: the ticket trader-broker's flatten would build, mirrored
                cover = {"symbol": sym, "side": "buy", "qty": ticketmod._str_num(abs(qty)), "type": "market",
                         "time_in_force": "day", "why": "flat by %s New York: covers the %s short before the close"
                         % (self.cfg.get("flat_by", "15:55"), sym)}
                wj(os.path.join(T, "orders.json"), shorts.orders_doc(cover, self.now_utc(), note="Cover ticket."))
                rc = shorts.risk_config(self.cfg, os.path.join(AP, "risk-config-short.json"))
                code, js, err = self.tool_json("risk", "--config", rc, timeout=60)
            else:
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
                if not short:
                    args += ["--filled-qty", fq, "--filled-price", fp]
                self.tool("ticket", *args)
                if short:
                    self.record_fill(t, "buy", fq, fp)
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
            "sim_equity": round(float(d.get("sim_equity") or 0) + float(d.get("realized") or 0), 2),
            "start_capital": (rj(os.path.join(AP, "account.json"), {}) or {}).get("start_capital") or self.cfg.get("budget"),
            "day": {"date": d.get("date"), "symbols": d.get("symbols", []), "in_play": d.get("in_play", []), "funnel": d.get("funnel", {}),
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
    sub.add_parser("export", help="write the weekly export zip (no keys, no personal data)")
    pp = sub.add_parser("profile", help="scale every limit to the money you will really trade with")
    pp.add_argument("capital", type=float)
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
    if a.cmd == "profile":
        import account_profile as prof
        lim = prof.apply(ROOT, a.capital)
        print("Konto-Profil $%.0f gesetzt:" % lim["budget"])
        print("  Risiko pro Trade      $%.2f (1 %%)" % lim["max_loss_per_trade_usd"])
        print("  Tagesverlust-Grenze   $%.2f (3 %%)" % lim["max_daily_loss_usd"])
        print("  größte Position       $%.2f (75 %%)" % lim["max_order_usd"])
        print("  Short                 %s" % ("an, solange das simulierte Konto mindestens $2.000 hat"
                                             if a.capital >= prof.SHORT_MIN_EQUITY else "aus (unter $2.000)"))
        print("Das simulierte Konto startet neu bei $%.0f. Bot neu starten (stop.bat, start.bat)." % lim["budget"])
        st = rj(os.path.join(AP, "account.json"), {}) or {}
        st.update({"start_capital": lim["budget"], "since": datetime.date.today().isoformat()})
        wj(os.path.join(AP, "account.json"), st)
        return 0
    if a.cmd == "export":
        import export
        path, man = export.write(ROOT)
        print("Export geschrieben: %s (%d Handelstage, %d Dateien)" % (path, len(man["days"]), len(man["files"])))
        print("Schick diese Datei zur Auswertung. Sie enthält keine Schlüssel und keine persönlichen Daten.")
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
