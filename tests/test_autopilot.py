"""The autopilot end to end, offline: the reel's TSLA day (2026-10-19) against a mock Alpaca.

Runs every pack tool for real on the saved fixtures; only the broker is a local mock."""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import mock_broker  # noqa: E402

import socket
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); PORT = _s.getsockname()[1]; _s.close()
SIM = {"scan": "tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz",
       "entry": "tests/fixtures/trader-entry/TSLA-2026-10-19-5m.json",
       "tm": "tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz",
       "news": "tests/fixtures/trader-news",
       "spy": "tests/fixtures/autopilot/SPY-60d5m.json.gz"}


ATR_AT_ENTRY = 3.2182     # 14-bar ATR of TSLA's 9:30-10:10 bars on 2026-10-19 (9 bars)
TRAIL_AT_1045 = 361.95    # 366.78 high - 1.5 x ATR, rounded down to the cent


def reflect(src, dst, R=730.0):
    """TSLA's real chart, mirrored around R: every breakout becomes a breakdown with the same numbers."""
    import gzip
    op = gzip.open if src.endswith(".gz") else open
    with op(src, "rt") as f:
        doc = json.load(f)
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    h, l = q["high"], q["low"]
    q["open"] = [None if v is None else R - v for v in q["open"]]
    q["close"] = [None if v is None else R - v for v in q["close"]]
    q["high"], q["low"] = [None if v is None else R - v for v in l], [None if v is None else R - v for v in h]
    for k in ("previousClose", "chartPreviousClose", "regularMarketPrice"):
        if isinstance(r["meta"].get(k), (int, float)):
            r["meta"][k] = R - r["meta"][k]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with (gzip.open if dst.endswith(".gz") else open)(dst, "wt") as f:
        json.dump(doc, f)


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


class AutopilotReel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_broker.serve(PORT, {"TSLA": 364.30})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # a throw-away copy of the project so trader/ starts empty
        self.d = tempfile.mkdtemp()
        for name in os.listdir(ROOT):
            if name.startswith("trader-") or name in ("autopilot", "tests"):
                src = os.path.join(ROOT, name)
                (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(self.d, name))
        os.makedirs(os.path.join(self.d, "trader"))
        cfg = json.load(open(os.path.join(ROOT, "trader-budget", "config.example.json")))
        json.dump(cfg, open(os.path.join(self.d, "trader", "config.json"), "w"))
        mock_broker.S.update(cash=100000.0, orders={}, positions={}, prices={"TSLA": 364.30})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def autopilot(self, ap):
        json.dump(dict({"entry_order": "market"}, **dict(ap, sim=SIM, symbols=["TSLA"], premarket_scan=False)),
                  open(os.path.join(self.d, "trader", "autopilot.json"), "w"))

    def tick(self, now):
        env = dict(os.environ, ALPACA_API_KEY="SIM", ALPACA_SECRET_KEY="SIM")
        p = subprocess.run([sys.executable, os.path.join(self.d, "autopilot", "autopilot.py"), "tick", "--sim",
                            "--now", now, "--broker-url", "http://127.0.0.1:%d" % PORT],
                           cwd=self.d, capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.load(open(os.path.join(self.d, "trader", "autopilot", "status.json"))), p.stdout

    def test_full_trade_target_then_flat_day(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        st, out = self.tick("2026-10-19 10:12")        # the 10:10 bar is still forming
        self.assertEqual(st["day"]["trades"], [])
        st, out = self.tick("2026-10-19 10:15")        # it closed above the range: the whole chain runs
        c = st["day"]["candidates"][0]
        self.assertEqual(c["symbol"], "TSLA")
        for stage in ("scanner", "news", "entry", "exit", "timemachine", "budget", "risk", "broker"):
            self.assertTrue(c["stages"][stage]["ok"], (stage, c["stages"]))
        self.assertEqual(c["plan"]["stop"], 358.32)
        self.assertEqual(c["plan"]["target"], 370.04)
        self.assertEqual(c["stages"]["budget"]["qty"], "8")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["qty"], t["fill_price"]), ("open", 8.0, 364.30))
        broker = json.load(open(os.path.join(self.d, "trader", "state", "broker.json")))
        kinds = [o["status"] for o in broker["orders"]]
        self.assertEqual(kinds, ["approved", "placed", "filled"])
        self.assertTrue(broker["orders"][0]["reply"].startswith("AUTOPILOT"))
        # the take-profit leg fills at the broker
        post("/mock/fill_leg", {"symbol": "TSLA", "type": "limit"})
        st, out = self.tick("2026-10-19 11:02")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["exit_price"], t["pnl"]), ("target", 370.04, 45.92))
        pf = json.load(open(os.path.join(self.d, "trader", "portfolio.json")))
        self.assertEqual(pf["positions"], [])

    def test_flat_by_close(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        self.tick("2026-10-19 10:15")
        post("/mock/price", {"TSLA": 366.10})
        st, out = self.tick("2026-10-19 15:55")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["exit_price"]), ("flat", 366.10))
        legs = [o for o in mock_broker.S["orders"].values() if o.get("parent")]
        self.assertTrue(all(o["status"] == "canceled" for o in legs))
        self.assertEqual(mock_broker.S["positions"], {})

    def test_time_machine_gate_blocks_weak_history(self):
        # default history gate: TSLA's same rule averaged -0.07 R over 33 past triggers
        self.autopilot({"target": "1R", "tm_replays": 2000, "tm_hist_min_avg_R": -0.05})
        st, out = self.tick("2026-10-19 10:15")
        c = st["day"]["candidates"][0]
        self.assertFalse(c["stages"]["timemachine"]["ok"])
        self.assertIn("averaged", c["why"])
        self.assertEqual(st["day"]["trades"], [])
        self.assertEqual(mock_broker.S["orders"], {})

    def test_pause_blocks_entries(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        os.makedirs(os.path.join(self.d, "trader", "autopilot"), exist_ok=True)
        open(os.path.join(self.d, "trader", "autopilot", "PAUSE"), "w").close()
        st, out = self.tick("2026-10-19 10:15")
        self.assertTrue(st["paused"])
        self.assertEqual(st["day"]["candidates"], [])

    def test_partial_exit_then_stop_at_entry(self):
        self.autopilot({"target": "2R", "exit_mode": "partial", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99,
                        "tm_min_avg_R": -9, "tm_replays": 2000})
        st, out = self.tick("2026-10-19 10:15")
        t = st["day"]["trades"][0]
        self.assertEqual((t["qty"], t["fill_price"], t["exit_mode"]), (8.0, 364.30, "partial"))
        post("/mock/price", {"TSLA": 370.30})             # +1R over the fill (364.30 + 5.86)
        st, out = self.tick("2026-10-19 10:40")
        t = st["day"]["trades"][0]
        self.assertEqual((t["exit_adjusted"], t["qty_open"], t["stop_now"]), ("partial", 4.0, 364.3))
        self.assertEqual(mock_broker.S["positions"]["TSLA"]["qty"], 4.0)
        post("/mock/fill_leg", {"symbol": "TSLA", "type": "stop"})   # the rest stops out at the entry
        st, out = self.tick("2026-10-19 11:30")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["pnl"], t["R"]), ("stop", 24.0, 0.51))
        self.assertNotIn("TSLA", mock_broker.S["positions"])
        pf = json.load(open(os.path.join(self.d, "trader", "portfolio.json")))
        self.assertEqual(pf["positions"], [])

    def test_breakeven_moves_the_stop(self):
        self.autopilot({"target": "2R", "exit_mode": "breakeven", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99,
                        "tm_min_avg_R": -9, "tm_replays": 2000})
        self.tick("2026-10-19 10:15")
        post("/mock/price", {"TSLA": 370.30})
        st, out = self.tick("2026-10-19 10:40")
        stop_leg = next(o for o in mock_broker.S["orders"].values() if o.get("type") == "stop")
        self.assertEqual(stop_leg["stop_price"], "364.30")
        self.assertEqual(st["day"]["trades"][0]["exit_adjusted"], "breakeven")
        post("/mock/fill_leg", {"symbol": "TSLA", "type": "stop"})
        st, out = self.tick("2026-10-19 11:30")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["pnl"]), ("stop", 0.0))

    def test_filters_are_measured_and_can_block(self):
        self.autopilot({"target": "1R", "rs_min": 50.0, "tm_replays": 2000})   # nobody beats SPY by 50 points
        st, out = self.tick("2026-10-19 10:15")
        c = st["day"]["candidates"][0]
        self.assertFalse(c["stages"]["entry"]["ok"])
        self.assertIn("relative strength", c["why"])
        f = c["features"]
        self.assertIsNotNone(f["rvol"])
        self.assertIsNotNone(f["rs"])
        self.assertEqual(st["day"]["trades"], [])

    def test_trailing_atr_stop_follows_the_trend(self):
        self.autopilot({"target": "2R", "exit_mode": "trail_atr", "exit_atr_k": 1.5, "tm_hist_min_avg_R": None,
                        "tm_min_edge_pts": -99, "tm_min_avg_R": -9, "tm_replays": 2000})
        st, out = self.tick("2026-10-19 10:15")
        t = st["day"]["trades"][0]
        tp = next(o for o in mock_broker.S["orders"].values() if o.get("parent") and o["type"] == "limit")
        self.assertEqual(tp["limit_price"], "%.2f" % (364.30 + 10 * 5.86))   # no fixed target: far cap
        self.assertAlmostEqual(t["atr"], ATR_AT_ENTRY, places=3)
        post("/mock/price", {"TSLA": 366.11})
        st, out = self.tick("2026-10-19 10:45")          # TSLA's real bars ran to 366.78 by then
        t = st["day"]["trades"][0]
        stop_leg = next(o for o in mock_broker.S["orders"].values() if o.get("type") == "stop")
        self.assertEqual(t["stop_now"], TRAIL_AT_1045)
        self.assertEqual(stop_leg["stop_price"], "%.2f" % TRAIL_AT_1045)
        self.assertIn("trailing stop", out)
        post("/mock/fill_leg", {"symbol": "TSLA", "type": "stop"})
        st, out = self.tick("2026-10-19 11:00")
        t = st["day"]["trades"][0]
        self.assertEqual(t["status"], "stop")       # 361.95 is under the fill 364.30: a loss, smaller than planned
        self.assertEqual(t["pnl"], round((TRAIL_AT_1045 - 364.30) * 8, 2))

    def short_setup(self, extra):
        fx = os.path.join(self.d, "tests", "fixtures")
        reflect(os.path.join(fx, "trader-entry", "TSLA-2026-10-19-5m.json"), os.path.join(self.d, "refl", "TSLA.json"))
        reflect(os.path.join(fx, "trader-timemachine", "TSLA-60d5m.json.gz"), os.path.join(self.d, "refl", "TSLA-60d.json.gz"))
        sim = dict(SIM, scan="refl/TSLA.json", entry="refl/TSLA.json", tm="refl/TSLA-60d.json.gz")
        ap = dict({"entry_order": "market", "sides": "short", "target": "1R", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99,
                   "tm_min_avg_R": -9, "tm_replays": 2000, "gap_filter": False}, **extra)
        json.dump(dict(ap, sim=sim, symbols=["TSLA"], premarket_scan=False),
                  open(os.path.join(self.d, "trader", "autopilot.json"), "w"))
        mock_broker.S["prices"]["TSLA"] = 365.70

    def test_short_breakdown_to_target(self):
        self.short_setup({})
        st, out = self.tick("2026-10-19 10:15")
        c = st["day"]["candidates"][0]
        for stage in ("scanner", "news", "entry", "exit", "timemachine", "budget", "risk", "broker"):
            self.assertTrue(c["stages"][stage]["ok"], (stage, c["stages"]))
        self.assertEqual((c["side"], c["plan"]["entry"], c["plan"]["stop"], c["plan"]["target"]),
                         ("sell", 365.82, 371.68, 359.96))           # the reel's numbers, mirrored
        t = st["day"]["trades"][0]
        self.assertEqual((t["side"], t["qty"], t["fill_price"]), ("sell", 8.0, 365.70))
        self.assertEqual(mock_broker.S["positions"]["TSLA"]["qty"], -8.0)
        pf = json.load(open(os.path.join(self.d, "trader", "portfolio.json")))
        self.assertEqual([p["shares"] for p in pf["positions"] if p["ticker"] == "TSLA"], [-8.0])
        risk = json.load(open(os.path.join(self.d, "trader", "state", "risk.json")))
        self.assertEqual(risk["results"][0]["verdict"], "PASS")
        cfg = json.load(open(os.path.join(self.d, "trader", "config.json")))
        self.assertFalse(cfg.get("allow_short"))                     # the user's config is untouched
        post("/mock/fill_leg", {"symbol": "TSLA", "type": "limit"})   # buy-to-cover at the target
        st, out = self.tick("2026-10-19 11:00")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["exit_price"], t["pnl"]), ("target", 359.96, 45.92))
        self.assertNotIn("TSLA", mock_broker.S["positions"])
        pf = json.load(open(os.path.join(self.d, "trader", "portfolio.json")))
        self.assertEqual([p for p in pf["positions"] if p["ticker"] == "TSLA"], [])

    def test_short_refused_when_hard_to_borrow(self):
        self.short_setup({})
        mock_broker.S["assets"] = {"TSLA": {"symbol": "TSLA", "shortable": True, "easy_to_borrow": False}}
        try:
            st, out = self.tick("2026-10-19 10:15")
        finally:
            mock_broker.S.pop("assets", None)
        c = st["day"]["candidates"][0]
        self.assertIn("hard to borrow", c["why"])
        self.assertEqual(mock_broker.S["orders"], {})

    def test_short_flat_by_close_and_partial(self):
        self.short_setup({"exit_mode": "partial", "target": "2R"})
        self.tick("2026-10-19 10:15")
        post("/mock/price", {"TSLA": 359.80})              # 1R in favour of the short (365.70 - 5.86)
        st, out = self.tick("2026-10-19 10:40")
        t = st["day"]["trades"][0]
        self.assertEqual((t["exit_adjusted"], t["qty_open"], t["stop_now"]), ("partial", 4.0, 365.7))
        self.assertEqual(mock_broker.S["positions"]["TSLA"]["qty"], -4.0)
        post("/mock/price", {"TSLA": 363.00})
        st, out = self.tick("2026-10-19 15:55")            # the rest is covered by the close
        t = st["day"]["trades"][0]
        self.assertEqual(t["status"], "flat")
        self.assertEqual(t["pnl"], round(4 * (365.70 - 359.80) + 4 * (365.70 - 363.00), 2))
        self.assertNotIn("TSLA", mock_broker.S["positions"])
        self.assertTrue(all(o["status"] != "new" for o in mock_broker.S["orders"].values()))

    def test_stocks_in_play_join_the_list(self):
        import datetime
        self.autopilot({"target": "1R", "sim_in_play": True})
        os.makedirs(os.path.join(self.d, "trader", "autopilot"), exist_ok=True)
        json.dump({"built": datetime.date.today().isoformat(), "symbols": [{"symbol": "NVDA"}, {"symbol": "KO"}]},
                  open(os.path.join(self.d, "trader", "autopilot", "liquid.json"), "w"))
        mock_broker.S["snapshots"] = {
            "NVDA": {"dailyBar": {"o": 206.0, "c": 207, "v": 4e6}, "prevDailyBar": {"c": 200.0, "v": 4e7}},
            "KO": {"dailyBar": {"o": 70.0, "c": 70, "v": 1e5}, "prevDailyBar": {"c": 70.0, "v": 1e7}}}
        self.tick("2026-10-19 09:20")
        st, out = self.tick("2026-10-19 09:46")
        self.assertEqual(st["day"]["symbols"], ["TSLA", "NVDA"])
        self.assertEqual(st["day"]["in_play"][0]["symbol"], "NVDA")
        self.assertIn("stocks in play: 1 found", out)

    def test_limit_entry_fills_or_is_missed(self):
        self.autopilot({"target": "2R", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99, "tm_min_avg_R": -9,
                        "tm_replays": 2000, "entry_order": "limit", "entry_limit_buffer_R": 0.1,
                        "entry_limit_timeout_min": 5})
        post("/mock/price", {"TSLA": 365.10})          # above the limit 364.18 + 0.586 = 364.77: no fill yet
        st, out = self.tick("2026-10-19 10:15")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["entry_order"], t["limit_price"]), ("pending", "limit", 364.77))
        o = mock_broker.S["orders"][t["order_id"]]
        self.assertEqual(o["qty"], "7")                  # sized from the limit: 50 / (364.77 - 358.32) = 7
        st, out = self.tick("2026-10-19 10:21")         # 6 minutes, still not filled: canceled
        t = st["day"]["trades"][0]
        self.assertEqual(t["status"], "missed")
        self.assertEqual(mock_broker.S["orders"][t["order_id"]]["status"], "canceled")
        self.assertEqual(mock_broker.S["positions"], {})

    def test_limit_entry_fills_when_price_comes_back(self):
        self.autopilot({"target": "2R", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99, "tm_min_avg_R": -9,
                        "tm_replays": 2000, "entry_order": "limit"})
        post("/mock/price", {"TSLA": 365.10})
        self.tick("2026-10-19 10:15")
        post("/mock/price", {"TSLA": 364.60})          # back under the limit: filled at 364.60
        st, out = self.tick("2026-10-19 10:17")
        t = st["day"]["trades"][0]
        self.assertEqual((t["status"], t["fill_price"], t["qty"]), ("open", 364.60, 7.0))

    def test_limit_with_1R_target_is_blocked_with_the_reason(self):
        # a limit above the signal makes reward:risk < 1 at a 1R target: trader-budget refuses, and says why
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000, "entry_order": "limit"})
        st, out = self.tick("2026-10-19 10:15")
        c = st["day"]["candidates"][0]
        self.assertIn("reward:risk 0.82", c["why"])
        self.assertEqual(st["day"]["trades"], [])

    def event_day(self, typ):
        json.dump({"events": [{"date": "2026-10-19", "type": typ, "name": "Test-Ereignis"}]},
                  open(os.path.join(self.d, "trader", "events.json"), "w"))

    def test_fed_day_is_flat_before_the_decision(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        self.event_day("fomc")
        st, out = self.tick("2026-10-19 10:15")
        self.assertIn("flat by 13:55", out)
        t = st["day"]["trades"][0]
        self.assertEqual(t["event"], "Test-Ereignis")
        post("/mock/price", {"TSLA": 366.00})
        st, out = self.tick("2026-10-19 13:55")             # not 15:55: before the 2 PM decision
        self.assertEqual(st["day"]["trades"][0]["status"], "flat")
        self.assertEqual(st["phase"], "flatten")

    def test_skip_day_and_earnings(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000,
                        "event_actions": {"cpi": "skip"}})
        self.event_day("cpi")
        st, out = self.tick("2026-10-19 10:15")
        self.assertEqual(st["day"]["candidates"], [])           # no scan at all on a skip day
        self.assertIn("no new entries today", out)

    def test_earnings_day_is_skipped(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        ap = json.load(open(os.path.join(self.d, "trader", "autopilot.json")))
        ap["sim"]["earnings"] = "earn.json"
        json.dump(ap, open(os.path.join(self.d, "trader", "autopilot.json"), "w"))
        json.dump({"data": {"rows": [{"symbol": "TSLA", "time": "time-after-hours"},
                                     {"symbol": "KO", "time": "time-pre-market"}]}},
                  open(os.path.join(self.d, "earn.json"), "w"))
        st, out = self.tick("2026-10-19 10:15")
        c = st["day"]["candidates"][0]
        self.assertFalse(c["stages"]["news"]["ok"])
        self.assertIn("earnings today (after the close)", c["why"])
        self.assertEqual(st["day"]["trades"], [])

    def test_setup_b_five_minute_range_on_stocks_in_play(self):
        import datetime
        # setup A is over at 10:00; only setup B (5-minute range, in-play stocks, entries until 10:30) can trade
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": None, "tm_min_edge_pts": -99, "tm_min_avg_R": -9,
                        "tm_replays": 2000, "last_entry": "10:00", "sim_in_play": True,
                        "setup_b": {"enabled": True, "or_minutes": 5, "last_entry": "10:30", "entries_from": "09:40",
                                    "only_in_play": True}})
        os.makedirs(os.path.join(self.d, "trader", "autopilot"), exist_ok=True)
        json.dump({"built": datetime.date.today().isoformat(), "symbols": [{"symbol": "TSLA"}]},
                  open(os.path.join(self.d, "trader", "autopilot", "liquid.json"), "w"))
        mock_broker.S["snapshots"] = {"TSLA": {"dailyBar": {"o": 360.5, "c": 359, "v": 6e6},
                                               "prevDailyBar": {"c": 354.72, "v": 9e7}}}
        try:
            self.tick("2026-10-19 09:36")                   # in play is ranked after the 5-minute range
            st, out = self.tick("2026-10-19 10:15")
        finally:
            mock_broker.S.pop("snapshots", None)
        self.assertEqual(st["day"]["in_play"][0]["symbol"], "TSLA")
        c = st["day"]["candidates"][0]
        self.assertEqual((c["setup"], c["or_minutes"]), ("ORB 5 min (in play)", 5))
        self.assertEqual(c["result"], "ORDER PLACED", c["stages"])
        t = st["day"]["trades"][0]
        self.assertEqual(t["setup"], "ORB 5 min (in play)")
        rules = json.load(open(os.path.join(self.d, "trader", "autopilot", "rules-b.json")))
        self.assertEqual(rules["no_entries_before"], "09:40")
        cfg = json.load(open(os.path.join(self.d, "trader", "config.json")))
        self.assertEqual(cfg["no_entries_before"], "09:45")    # the user's rules are unchanged

    def test_account_profile_and_short_minimum(self):
        env = dict(os.environ, ALPACA_API_KEY="SIM", ALPACA_SECRET_KEY="SIM")
        p = subprocess.run([sys.executable, os.path.join(self.d, "autopilot", "autopilot.py"), "profile", "2000"],
                           cwd=self.d, capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = json.load(open(os.path.join(self.d, "trader", "config.json")))
        self.assertEqual((cfg["budget"], cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"], cfg["max_order_usd"]),
                         (2000.0, 20.0, 60.0, 1500.0))
        # a past losing day leaves the simulated account at $1,950: no shorts today, longs still trade
        os.makedirs(os.path.join(self.d, "trader", "autopilot", "days"), exist_ok=True)
        json.dump({"date": "2026-10-16", "trades": [{"symbol": "X", "status": "stop", "pnl": -50.0, "R": -1}]},
                  open(os.path.join(self.d, "trader", "autopilot", "days", "2026-10-16.json"), "w"))
        acc = os.path.join(self.d, "trader", "autopilot", "account.json")
        a = json.load(open(acc)); a["since"] = "2026-01-01"; json.dump(a, open(acc, "w"))
        self.short_setup({})
        st, out = self.tick("2026-10-19 10:15")
        self.assertEqual(st["sim_equity"], 1950.0)
        self.assertIn("short selling off today", out)
        self.assertEqual(st["day"]["candidates"], [])
        pf = json.load(open(os.path.join(self.d, "trader", "portfolio.json")))
        self.assertEqual(pf["cash"], 1950.0)

    def test_refuses_live(self):
        cfgp = os.path.join(self.d, "trader", "config.json")
        cfg = json.load(open(cfgp)); cfg["paper"] = False; json.dump(cfg, open(cfgp, "w"))
        self.autopilot({})
        env = dict(os.environ, ALPACA_API_KEY="SIM", ALPACA_SECRET_KEY="SIM")
        p = subprocess.run([sys.executable, os.path.join(self.d, "autopilot", "autopilot.py"), "tick"],
                           cwd=self.d, capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("paper", p.stderr + p.stdout)
        sys.path.insert(0, os.path.join(self.d, "autopilot"))
        from alpaca import Alpaca, AlpacaError
        with self.assertRaises(AlpacaError):
            Alpaca("k", "s", base_url="https://api.alpaca.markets")


if __name__ == "__main__":
    unittest.main()
