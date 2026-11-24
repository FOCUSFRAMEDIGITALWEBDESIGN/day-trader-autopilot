"""Stocks in play against the mock Alpaca data API."""
import json, os, socket, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import mock_broker  # noqa: E402
import inplay  # noqa: E402
from alpaca import Alpaca  # noqa: E402


def daily(price, vol, n=20):
    return [{"t": "2026-10-%02dT04:00:00Z" % (i + 1), "o": price, "h": price, "l": price, "c": price, "v": vol}
            for i in range(n)]


class InPlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        cls.srv = mock_broker.serve(cls.port, {})
        cls.api = Alpaca("k", "s", base_url="http://127.0.0.1:%d" % cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_liquid_universe_and_ranking(self):
        mock_broker.S["daily"] = {"BIG": daily(100, 5e6), "MID": daily(50, 2e6), "PENNY": daily(3, 9e7),
                                  "THIN": daily(80, 1e4, n=5)}
        with tempfile.TemporaryDirectory() as d:
            doc = inplay.build_liquid(self.api, ["BIG", "MID", "PENNY", "THIN"], os.path.join(d, "l.json"),
                                      keep=10, min_price=10, log=lambda m: None)
            self.assertEqual([r["symbol"] for r in doc["symbols"]], ["BIG", "MID"])   # under $10 / too new are out
            self.assertEqual(json.load(open(os.path.join(d, "l.json")))["symbols"][0]["dollar_volume"], 5e8)
        # 15 minutes in: 15/390 of yesterday's volume is normal
        mock_broker.S["snapshots"] = {
            "BIG": {"dailyBar": {"o": 104.0, "c": 105, "v": 600000}, "prevDailyBar": {"c": 100.0, "v": 5e6},
                    "latestTrade": {"p": 105.2}},                                    # +4% gap, 3.1x volume
            "MID": {"dailyBar": {"o": 49.0, "c": 48, "v": 300000}, "prevDailyBar": {"c": 50.0, "v": 2e6}},   # -2%, 3.9x
            "FLAT": {"dailyBar": {"o": 50.1, "c": 50, "v": 900000}, "prevDailyBar": {"c": 50.0, "v": 2e6}},  # no gap
        }
        got = inplay.rank(self.api, [{"symbol": s} for s in ("BIG", "MID", "FLAT")], 15, min_gap=1, min_rvol=1.5)
        # score = rvol x (1 + |gap| / 4): BIG 3.12 x 2.0 = 6.24 beats MID 3.9 x 1.5 = 5.85; FLAT has no gap
        self.assertEqual([r["symbol"] for r in got], ["BIG", "MID"])
        self.assertEqual((got[0]["gap"], got[0]["rvol"], got[0]["price"]), (4.0, 3.12, 105.2))
        self.assertEqual(got[1]["gap"], -2.0)                                        # gaps down count too (shorts)


if __name__ == "__main__":
    unittest.main()
