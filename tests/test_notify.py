"""Telegram messages against a local fake Telegram API."""
import importlib, json, os, socket, sys, tempfile, threading, unittest, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
SENT = []


class FakeTelegram(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        params = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
        method = self.path.rsplit("/", 1)[1]
        SENT.append((self.path.split("/")[1], method, params))
        body = {"ok": True, "result": []}
        if method == "getUpdates":
            body["result"] = [{"update_id": 1, "message": {"chat": {"id": 4242}, "text": "/start"}}]
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Notify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", port), FakeTelegram)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["TELEGRAM_API"] = "http://127.0.0.1:%d" % port
        import notify
        cls.n = importlib.reload(notify)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for k in ("TELEGRAM_API", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)

    def setUp(self):
        SENT.clear()
        self.d = tempfile.mkdtemp()
        self.n.ROOT = self.d
        self.n._last.clear()
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    def test_quiet_without_setup(self):
        self.assertFalse(self.n.send("hello", wait=True))
        self.assertEqual(SENT, [])

    def test_setup_writes_env_and_sends(self):
        open(os.path.join(self.d, ".env"), "w").write("ALPACA_API_KEY=PKX\n")
        self.assertEqual(self.n.setup("123:ABC", seconds=5), 0)
        env = open(os.path.join(self.d, ".env")).read()
        self.assertIn("ALPACA_API_KEY=PKX", env)                 # the keys stay
        self.assertIn("TELEGRAM_CHAT_ID=4242", env)
        self.assertEqual(SENT[-1][1], "sendMessage")
        self.assertEqual(SENT[-1][0], "bot123:ABC")
        self.assertEqual(SENT[-1][2]["chat_id"], "4242")

    def test_dedupe_by_key(self):
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "1:X", "7"
        self.assertTrue(self.n.send("err A", key="e", wait=True))
        self.assertFalse(self.n.send("err A again", key="e", wait=True))
        self.assertTrue(self.n.send("trade", wait=True))
        self.assertEqual([p[2]["text"] for p in SENT], ["err A", "trade"])


if __name__ == "__main__":
    unittest.main()
