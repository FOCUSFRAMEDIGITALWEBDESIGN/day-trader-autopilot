"""Telegram control against a fake Telegram API: only your chat, commands, /flat, /export."""
import json, os, shutil, socket, sys, tempfile, threading, unittest, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import telegram_bot as tb  # noqa: E402

INBOX, SENT = [], []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        method = self.path.rsplit("/", 1)[1]
        if method == "getUpdates":
            res = list(INBOX); INBOX.clear()
            body = {"ok": True, "result": res}
        elif method == "sendDocument":
            SENT.append(("document", raw))
            body = {"ok": True}
        else:
            SENT.append((method, dict(urllib.parse.parse_qsl(raw.decode()))))
            body = {"ok": True}
        out = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)


def msg(chat, text, uid=[0]):
    uid[0] += 1
    return {"update_id": uid[0], "message": {"chat": {"id": chat}, "text": text}}


class TelegramControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", port), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = "http://127.0.0.1:%d" % port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        INBOX.clear(); SENT.clear()
        self.d = tempfile.mkdtemp()
        self.old = (tb.ROOT, tb.AP)
        tb.ROOT, tb.AP = self.d, os.path.join(self.d, "trader", "autopilot")
        os.makedirs(tb.AP)
        json.dump({"updated": "2026-10-19 10:20:00", "phase": "trading", "paused": False, "sim_equity": 2045.5,
                   "start_capital": 2000, "day": {"realized": 45.5, "candidates": [
                       {"first_seen": "10:15", "symbol": "TSLA", "result": "ORDER PLACED"}],
                       "trades": [{"symbol": "TSLA", "side": "buy", "status": "open", "qty": 3, "fill_price": 364.3,
                                   "last": 366.0, "plan": {"stop": 358.32, "target": 375.9}, "at": "10:15"}]}},
                  open(os.path.join(tb.AP, "status.json"), "w"))
        self.bot = tb.Bot(tb.Telegram("1:X", "777", api=self.api))
        self.bot.weekly_export = lambda: None

    def tearDown(self):
        tb.ROOT, tb.AP = self.old
        shutil.rmtree(self.d, ignore_errors=True)

    def texts(self):
        return [p["text"] for m, p in SENT if m == "sendMessage"]

    def test_only_my_chat_and_status(self):
        INBOX.extend([msg(999, "/pause"), msg(777, "/status")])
        self.bot.run(once=True)
        self.assertFalse(os.path.exists(os.path.join(tb.AP, "PAUSE")))      # a stranger cannot pause
        t = self.texts()
        self.assertEqual(len(t), 1)
        self.assertIn("Simuliertes Konto: $2.045,50", t[0])
        self.assertIn("TSLA Long 3 Stk @ 364,30 · Stop 358,32 · Ziel 375,90", t[0])
        self.assertIn("+$5,10", t[0])

    def test_pause_resume_and_flat_needs_confirmation(self):
        INBOX.extend([msg(777, "/pause"), msg(777, "/flat")])
        self.bot.run(once=True)
        self.assertTrue(os.path.exists(os.path.join(tb.AP, "PAUSE")))
        self.assertFalse(os.path.exists(os.path.join(tb.AP, "FLATTEN_NOW")))   # not without "ja"
        INBOX.append(msg(777, "/flat ja"))
        self.bot.run(once=True)
        self.assertTrue(os.path.exists(os.path.join(tb.AP, "FLATTEN_NOW")))
        INBOX.append(msg(777, "/weiter"))
        self.bot.run(once=True)
        self.assertFalse(os.path.exists(os.path.join(tb.AP, "PAUSE")))

    def test_export_is_sent_as_a_document(self):
        os.makedirs(os.path.join(self.d, "trader", "autopilot", "days"))
        json.dump({"date": "2026-10-19", "trades": []}, open(os.path.join(tb.AP, "days", "2026-10-19.json"), "w"))
        INBOX.append(msg(777, "/export"))
        self.bot.run(once=True)
        docs = [raw for m, raw in SENT if m == "document"]
        self.assertEqual(len(docs), 1)
        self.assertIn(b'filename="autopilot-export-', docs[0])
        self.assertIn(b"trader/autopilot/days/2026-10-19.json", docs[0])
        self.assertIn(b"PK", docs[0])                                         # a zip


if __name__ == "__main__":
    unittest.main()
