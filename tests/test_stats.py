import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "autopilot"))
import stats  # noqa: E402


class Stats(unittest.TestCase):
    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "days"))
            trades = [{"symbol": "A", "side": "buy", "status": "target", "at": "10:20", "pnl": 100, "R": 2},
                      {"symbol": "B", "side": "sell", "status": "stop", "at": "9:50", "pnl": -50, "R": -1},
                      {"symbol": "C", "side": "buy", "status": "open", "at": "11:00", "pnl": None}]
            json.dump({"date": "2026-11-23", "trades": trades, "in_play": [{"symbol": "A"}],
                       "candidates": {"A": {"symbol": "A", "result": "ORDER PLACED", "stages": {}},
                                      "Z": {"symbol": "Z", "stages": {"timemachine": {"ok": False}},
                                            "why": "edge -3 pts under the 0 needed"}}},
                      open(os.path.join(d, "days", "2026-11-23.json"), "w"))
            json.dump([{"current_eval": {"mean": 0.3}}], open(os.path.join(d, "learning.json"), "w"))
            r = stats.report(d)
        o = r["overall"]
        self.assertEqual((o["n"], o["pnl"], o["avg_R"], o["win_pct"], o["max_drawdown"]), (2, 50, 0.5, 50.0, 50))
        self.assertEqual(r["vs_backtest"], 0.2)
        g = {x["title"]: x for x in r["groups"]}
        self.assertEqual([x["label"] for x in g["Uhrzeit des Einstiegs (New York)"]["rows"]], ["9:45–10:15", "10:15–10:45"])
        self.assertEqual({x["label"]: x["n"] for x in g["Stock in play"]["rows"]}, {"ja": 1, "nein": 1})
        self.assertEqual(r["funnel"]["stopped_at"], {"Order": 1, "timemachine": 1})


if __name__ == "__main__":
    unittest.main()
