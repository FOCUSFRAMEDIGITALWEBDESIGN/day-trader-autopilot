"""The learner's guard rails, offline."""
import json, os, shutil, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import learn  # noqa: E402


class Learn(unittest.TestCase):
    def test_settings_map_to_the_backtest(self):
        p = learn.to_bt({"or_minutes": 15, "stop": "or-mid", "target": "2R", "last_entry": "11:30",
                         "max_pct_over": 0.75, "min_volume_x": None, "gap_filter": True})
        self.assertEqual((p["stop"], p["target_r"], p["last_entry_slot"], p["min_volume_x"]), ("or_mid", 2.0, 23, 0.0))

    def test_summary_lower_bound(self):
        s = learn.summary([1.0, -1.0, 2.0, -1.0])
        self.assertEqual((s["n"], s["mean"]), (4, 0.25))
        self.assertLess(s["lcb"], s["mean"])

    def test_live_guard_pauses_a_losing_plan(self):
        d = tempfile.mkdtemp()
        try:
            old = (learn.T, learn.AP)
            learn.T, learn.AP = os.path.join(d, "trader"), os.path.join(d, "trader", "autopilot")
            os.makedirs(learn.AP)
            json.dump({"strategy_version": 3, "symbols": []}, open(os.path.join(learn.T, "autopilot.json"), "w"))
            trades = [{"R": -0.8, "strategy_version": 3}] * 16 + [{"R": 2.0, "strategy_version": 2}] * 10
            json.dump([{"date": "2026-11-20", "trades": trades}], open(os.path.join(learn.AP, "history.json"), "w"))
            old_data = learn.DATA
            learn.DATA = os.path.join(d, "nodata")
            learn.main(["--no-fetch"])
            learn.DATA = old_data
            self.assertTrue(os.path.exists(os.path.join(learn.AP, "PAUSE")))
            rec = json.load(open(os.path.join(learn.AP, "learning.json")))[-1]
            self.assertEqual(rec["live"]["n"], 16)          # only the current plan's trades count
            self.assertFalse(rec["changed"])                 # no data -> no switch
            cfg = json.load(open(os.path.join(learn.T, "autopilot.json")))
            self.assertEqual(cfg["strategy_version"], 3)
        finally:
            learn.T, learn.AP = old
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
