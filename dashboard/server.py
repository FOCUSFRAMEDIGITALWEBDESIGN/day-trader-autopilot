#!/usr/bin/env python3
"""The autopilot's dashboard: a small local web server (standard library only).

    python3 dashboard/server.py                    # http://127.0.0.1:8765
    python3 dashboard/server.py --host 0.0.0.0 --port 8765   # reachable on your LAN (set DASHBOARD_TOKEN!)

It reads the files the autopilot writes in trader/autopilot/ (it never needs the
Alpaca keys) and offers one control: pause / resume new entries.
If DASHBOARD_TOKEN is set, every request needs ?token=... once (it is then kept in a cookie).
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AP = os.path.join(ROOT, "trader", "autopilot")
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("DASHBOARD_TOKEN")


def rj(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tail_jsonl(path, n):
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def state():
    s = rj(os.path.join(AP, "status.json"), {}) or {}
    s["history"] = rj(os.path.join(AP, "history.json"), []) or []
    s["equity"] = tail_jsonl(os.path.join(AP, "equity.jsonl"), 3000)
    s["events"] = list(reversed(tail_jsonl(os.path.join(AP, "events.jsonl"), 150)))
    for e in s["events"]:
        e.pop("data", None) if e.get("level") != "error" else None
    s["pause_file"] = os.path.exists(os.path.join(AP, "PAUSE"))
    try:
        with open(os.path.join(AP, "PAUSE"), encoding="utf-8") as f:
            s["pause_reason"] = f.read().strip()
    except OSError:
        s["pause_reason"] = None
    s["learning"] = (rj(os.path.join(AP, "learning.json"), []) or [])[-12:]
    return s


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [None])[0] == TOKEN:
            return True
        return ("dtoken=" + TOKEN) in (self.headers.get("Cookie") or "")

    def _send(self, code, body, ctype="application/json", cookie=False):
        raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if cookie and TOKEN:
            self.send_header("Set-Cookie", "dtoken=%s; HttpOnly; SameSite=Strict; Path=/" % TOKEN)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._authed():
            return self._send(401, b"token required: open /?token=YOUR_DASHBOARD_TOKEN", "text/plain")
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8", cookie=True)
        if p == "/api/state":
            return self._send(200, state())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "token required"})
        p = urlparse(self.path).path
        flag = os.path.join(AP, "PAUSE")
        os.makedirs(AP, exist_ok=True)
        if p == "/api/pause":
            with open(flag, "w") as f:
                f.write("paused from the dashboard\n")
            return self._send(200, {"paused": True})
        if p == "/api/resume":
            if os.path.exists(flag):
                os.remove(flag)
            return self._send(200, {"paused": False})
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and not TOKEN:
        sys.exit("refusing to listen on %s without DASHBOARD_TOKEN set" % a.host)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("dashboard on http://%s:%d" % (a.host, a.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
