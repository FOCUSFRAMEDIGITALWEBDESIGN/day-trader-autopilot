import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "autopilot"))
import features as fx  # noqa: E402


def bar(o, h, l, c, v=100):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


class Exits(unittest.TestCase):
    # fill 100, stop 98 (risk 2), target 104 (2R), trigger for breakeven / partial at 102
    def test_fixed(self):
        self.assertEqual(fx.simulate_exit([bar(100, 105, 99, 104)], 100, 98, 104, 2, "fixed"), 2.0)
        self.assertEqual(fx.simulate_exit([bar(100, 101, 97, 98)], 100, 98, 104, 2, "fixed"), -1.0)
        self.assertEqual(fx.simulate_exit([bar(100, 101, 99, 101)], 100, 98, 104, 2, "fixed"), 0.5)

    def test_breakeven(self):
        bars = [bar(100, 102.5, 99, 102), bar(102, 102, 99.5, 100)]
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "breakeven"), 0.0)
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "fixed"), 0.0)   # closes at 100 either way
        bars = [bar(100, 102.5, 99, 102), bar(102, 102, 98.5, 99)]
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "breakeven"), 0.0)
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "fixed"), -0.5)

    def test_partial(self):
        bars = [bar(100, 102.5, 99, 102), bar(102, 104.5, 101, 104)]
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "partial"), 1.5)   # half at 1R, half at 2R
        bars = [bar(100, 102.5, 99, 102), bar(102, 102, 99.5, 99.8)]
        self.assertEqual(fx.simulate_exit(bars, 100, 98, 104, 2, "partial"), 0.5)   # half at 1R, half at entry


class Filters(unittest.TestCase):
    def test_rvol_and_strength(self):
        past = [[bar(10, 10, 10, 10, 100)] * 3 for _ in range(10)]
        today = [bar(10, 11, 10, 11, 300)] * 3
        self.assertAlmostEqual(fx.rvol(today, past, 3), 3.0)
        self.assertIsNone(fx.rvol(today, past[:3], 3))
        spy = [bar(100, 100, 100, 100.5)] * 3
        self.assertAlmostEqual(fx.rel_strength(today, spy, 2), 10.0 - 0.5)
        self.assertTrue(fx.market_ok(spy, 2, "green"))
        ok, why = fx.passes({"rvol": 1.2, "market_ok": True, "rs": 1}, {"rvol_min": 1.5})
        self.assertFalse(ok)
        self.assertTrue(fx.passes({"rvol": 2, "market_ok": True, "rs": 1}, {"rvol_min": 1.5, "rs_min": 0})[0])


if __name__ == "__main__":
    unittest.main()


class Trailing(unittest.TestCase):
    # fill 100, stop 98, risk 2
    def test_r_steps(self):
        self.assertEqual(fx.trail_level("trail_r", 100, 98, 2, 101.9), 98.0)
        self.assertEqual(fx.trail_level("trail_r", 100, 98, 2, 102.0), 100.0)   # +1R: stop to the fill
        self.assertEqual(fx.trail_level("trail_r", 100, 98, 2, 104.5), 102.0)   # +2R: stop at +1R
        self.assertEqual(fx.trail_level("trail_r", 100, 102, 2, 102.5), 102.0)  # never down

    def test_atr(self):
        self.assertEqual(fx.trail_level("trail_atr", 100, 98, 2, 103, atr_value=1.0, k=2), 101.0)
        self.assertEqual(fx.trail_level("trail_atr", 100, 98, 2, 99.5, atr_value=1.0, k=2), 98.0)
        self.assertAlmostEqual(fx.atr([bar(10, 11, 9, 10), bar(10, 12, 10, 11)]), 2.0)

    def test_simulated_trades(self):
        up = [bar(100, 102.5, 99.5, 102), bar(102, 104.5, 101.5, 104), bar(104, 106.2, 103.5, 106), bar(106, 106, 103.9, 104)]
        # R steps: after 106.2 (+3.1R) the stop is 104 (+2R); the last bar's low 103.9 hits it
        self.assertEqual(fx.simulate_exit(up, 100, 98, 999, 2, "trail_r"), 2.0)
        # a fixed 2R target would have stopped at 104 too, but the trend could run further:
        run = up[:3] + [bar(106, 110, 105.5, 109.5)]
        self.assertEqual(fx.simulate_exit(run, 100, 98, 999, 2, "trail_r"), 4.75)       # out at the close 109.5
        self.assertEqual(fx.simulate_exit(run, 100, 98, 104, 2, "fixed"), 2.0)
        # ATR 1, k 2: stop trails 2 under the high; bar 4's low 103.9 < 106.2-2=104.2 -> out at 104.2
        self.assertAlmostEqual(fx.simulate_exit(up, 100, 98, 999, 2, "trail_atr", atr_value=1.0, k=2), 2.1)
