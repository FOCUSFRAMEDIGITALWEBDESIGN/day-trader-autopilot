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
