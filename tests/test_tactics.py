"""The tactic toolbox: the same rules as the research, the new families on synthetic days, live data."""
import gzip, json, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
sys.path.insert(0, os.path.join(ROOT, "tuning"))
import tactics  # noqa: E402
import features as fx  # noqa: E402
import research  # noqa: E402
import backtest as bt  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")
SPY = os.path.join(ROOT, "tests", "fixtures", "autopilot", "SPY-60d5m.json.gz")


def load():
    out = {}
    for sym, p in (("TSLA", FIX), ("SPY", SPY)):
        out[sym] = [(d, [fx.compact(b) for b in D]) for d, D in bt.sessions_of(p)]
    return out


def bar(o, h, l, c, v=1000):
    return fx.Bar(o, h, l, c, v)


class Tactics(unittest.TestCase):
    def test_orb_matches_the_research_rows(self):
        data = load()
        res = tactics.run(data, tactics=[t for t in tactics.TACTICS if t["name"] == "ORB 15 2R"])["ORB 15 2R"]
        rows = [r for r in research.build_rows(data, 15, "11:30")
                if research.keep(r, {"sides": "both", "max_pct_over": 0.75, "gap_filter": True})
                and ("or-mid", "2R", "fixed") in r["R"]]
        self.assertGreater(len(rows), 10)
        self.assertEqual(len(res), len(rows))
        self.assertAlmostEqual(sum(t["R"] for t in res), sum(r["R"][("or-mid", "2R", "fixed")] for r in rows), places=5)

    def test_vwap_pullback_on_a_synthetic_trend_day(self):
        D = [bar(100 + i * 0.2, 100.3 + i * 0.2, 99.9 + i * 0.2, 100.2 + i * 0.2) for i in range(10)]
        D.append(bar(102.0, 102.1, 100.4, 101.9))          # bar 10 dips through VWAP and closes back above
        D += [bar(102 + i * 0.3, 102.4 + i * 0.3, 101.9 + i * 0.3, 102.3 + i * 0.3) for i in range(70)]
        t = tactics.vwap_pullback("X", "2026-01-05", "buy", D, 99.5, [], None, {"name": "VWAP-Rücksetzer"})
        self.assertIsNotNone(t)
        self.assertEqual(t["bar"], 10)
        self.assertGreater(t["R"], 0)                        # the trend went on: the 2R target was hit

    def test_gap_fade_on_a_synthetic_gap_down(self):
        # gapped down 3 % from 100, recovers above VWAP and the 30-minute middle by 10:00, fills the gap
        D = [bar(97.0, 97.3, 96.6, 96.8)] + [bar(96.8 + i * 0.2, 97.2 + i * 0.2, 96.7 + i * 0.2, 97.0 + i * 0.2)
                                             for i in range(5)]
        D += [bar(98.0 + i * 0.1, 98.3 + i * 0.1, 97.9 + i * 0.1, 98.2 + i * 0.1) for i in range(72)]
        t = tactics.gap_fade("X", "2026-01-05", "buy", D, 100.0, [], None, {"name": "Gap-Fade"})
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t["R"], (100.0 - D[6]["o"]) / (D[5]["c"] - 96.6), places=4)
        # no gap, no trade
        self.assertIsNone(tactics.gap_fade("X", "2026-01-05", "buy", D, 97.0, [], None, {"name": "Gap-Fade"}))

    def test_short_side_is_the_mirror(self):
        data = load()
        res = tactics.run(data, sides=("sell",))
        self.assertTrue(all(t["side"] == "sell" for v in res.values() for t in v))
        self.assertTrue(any(res.values()))

    def test_sessions_from_alpaca_bars(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_alpaca_data import yahoo_to_alpaca_bars
        S = tactics.sessions_from_alpaca(yahoo_to_alpaca_bars(FIX))
        ref = bt.sessions_of(FIX)
        self.assertEqual([d for d, _ in S], [d for d, _ in ref])
        self.assertAlmostEqual(S[-1][1][40]["c"], ref[-1][1][40]["c"])


if __name__ == "__main__":
    unittest.main()
