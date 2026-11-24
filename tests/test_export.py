"""The weekly export (no secrets) and the analysis of it, on a simulated TSLA day."""
import json, os, shutil, subprocess, sys, tempfile, unittest, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tuning"))
import test_autopilot as ta  # noqa: E402  (reuses its mock broker and project copy)
import analyze_export  # noqa: E402


class Export(unittest.TestCase):
    setUpClass = classmethod(ta.AutopilotReel.setUpClass.__func__)
    tearDownClass = classmethod(ta.AutopilotReel.tearDownClass.__func__)
    setUp, tearDown = ta.AutopilotReel.setUp, ta.AutopilotReel.tearDown
    autopilot, tick = ta.AutopilotReel.autopilot, ta.AutopilotReel.tick

    def test_export_and_analysis(self):
        self.autopilot({"target": "1R", "tm_hist_min_avg_R": -0.10, "tm_replays": 2000})
        cfgp = os.path.join(self.d, "trader", "config.json")
        cfg = json.load(open(cfgp)); cfg["sec_user_agent"] = "Max Muster max@example.de"; json.dump(cfg, open(cfgp, "w"))
        open(os.path.join(self.d, ".env"), "w").write("ALPACA_SECRET_KEY=TOPSECRET123\n")
        self.tick("2026-10-19 10:15")
        ta.post("/mock/fill_leg", {"symbol": "TSLA", "type": "limit"})
        self.tick("2026-10-19 11:00")
        self.tick("2026-10-20 09:20")                     # the next day: 2026-10-19 is saved in full
        self.assertTrue(os.path.exists(os.path.join(self.d, "trader", "autopilot", "days", "2026-10-19.json")))
        p = subprocess.run([sys.executable, os.path.join(self.d, "autopilot", "autopilot.py"), "export"],
                           cwd=self.d, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        zpath = os.path.join(self.d, "trader", "export", os.listdir(os.path.join(self.d, "trader", "export"))[0])
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
        self.assertIn("trader/autopilot/days/2026-10-19.json", names)
        self.assertNotIn(b"TOPSECRET123", blob)
        self.assertNotIn(b"max@example.de", blob)
        self.assertFalse(any(n.endswith(".env") for n in names))
        # the analysis, with TSLA's real candles of that morning
        cdir = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(ROOT, "tests", "fixtures", "trader-entry", "TSLA-2026-10-19-5m.json"),
                        os.path.join(cdir, "TSLA.json"))
            text, data = analyze_export.analyze(zpath, cdir)
        finally:
            shutil.rmtree(cdir, ignore_errors=True)
        self.assertEqual(data["report"]["overall"]["n"], 1)
        self.assertEqual(data["report"]["overall"]["pnl"], 45.92)
        self.assertEqual(data["counterfactual"]["traded"]["n"], 1)
        self.assertAlmostEqual(data["slippage_R"], (364.30 - 364.18) / 5.86, places=4)
        self.assertIn("Gehandelt", text)
        groups = {g["title"]: g for g in data["report"]["groups"]}
        self.assertEqual(groups["Richtung"]["rows"][0]["label"], "Long")


if __name__ == "__main__":
    unittest.main()
