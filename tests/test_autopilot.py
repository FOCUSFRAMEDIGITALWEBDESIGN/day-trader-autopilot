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
        json.dump(dict(ap, sim=SIM, symbols=["TSLA"], premarket_scan=False),
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
