"""Dashboard server: chart candles in New York time, static files, input checks."""
import calendar, importlib.util, json, os, shutil, socket, tempfile, threading, unittest, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_server(chart_dir):
    os.environ["DASHBOARD_CHART_DIR"] = chart_dir
    os.environ.pop("DASHBOARD_TOKEN", None)
    spec = importlib.util.spec_from_file_location("dash_server_t", os.path.join(ROOT, "dashboard", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "tests", "fixtures", "trader-entry", "TSLA-2026-10-19-5m.json"),
                    os.path.join(cls.d, "TSLA.json"))
        cls.srv_mod = load_server(cls.d)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        cls.srv = cls.srv_mod.ThreadingHTTPServer(("127.0.0.1", cls.port), cls.srv_mod.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("DASHBOARD_CHART_DIR", None)
        shutil.rmtree(cls.d, ignore_errors=True)

    def get(self, path):
        return urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path))

    def test_chart_candles_in_new_york_time(self):
        js = json.loads(self.get("/api/chart?symbol=TSLA&date=2026-10-19&interval=5m").read())
        bars = js["bars"]
        self.assertEqual(bars[0]["time"], calendar.timegm((2026, 10, 19, 9, 30, 0)))   # 9:30 on the chart's clock
        self.assertEqual((bars[0]["high"], bars[0]["low"]), (362.18, 354.47))         # the reel's opening range
        self.assertTrue(all(b["time"] % 300 == 0 for b in bars))
        self.assertEqual(round(bars[8]["close"], 2), 364.18)                           # the 10:10 trigger bar

    def test_bad_input_is_refused(self):
        for q in ("symbol=../../etc&date=2026-10-19", "symbol=TSLA&date=19.10.2026"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get("/api/chart?" + q)
            self.assertEqual(e.exception.code, 502)

    def test_static_files(self):
        self.assertIn(b"Lightweight Charts", self.get("/vendor/lightweight-charts.js").read(400))
        self.assertIn(b"TradeChart", self.get("/tradechart.js").read())
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/vendor/..%2Fserver.py")
        page = self.get("/").read().decode()
        self.assertIn("vendor/lightweight-charts.js", page)


if __name__ == "__main__":
    unittest.main()
