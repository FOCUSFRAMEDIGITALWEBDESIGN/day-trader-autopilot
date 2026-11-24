#!/usr/bin/env python3
"""The autopilot's dashboard: a small local web server (standard library only).

    python3 dashboard/server.py                    # http://127.0.0.1:8765
    python3 dashboard/server.py --host 0.0.0.0 --port 8765   # reachable on your LAN (set DASHBOARD_TOKEN!)

It reads the files the autopilot writes in trader/autopilot/ (it never needs the
Alpaca keys) and offers one control: pause / resume new entries.
If DASHBOARD_TOKEN is set, every request needs ?token=... once (it is then kept in a cookie).
"""
import argparse, calendar, datetime, gzip, importlib.util, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AP = os.path.join(ROOT, "trader", "autopilot")
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("DASHBOARD_TOKEN")
CHART_DIR = os.environ.get("DASHBOARD_CHART_DIR")   # tests / replays: saved Yahoo chart files, <SYM>.json(.gz)
_CACHE, _LOCK = {}, threading.Lock()


def _mod(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


entrymod = _mod("dash_entry", "trader-entry/entry.py")
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import stats  # noqa: E402


def chart(symbol, date, interval="5m"):
    """Regular-session candles of one symbol on one New York date, for the chart window.
    Times are New York wall-clock seconds (what the chart shows), not UTC."""
    if not re.match(r"^[A-Z][A-Z.\-]{0,9}$", symbol) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("bad symbol or date")
    if interval not in ("1m", "5m"):
        interval = "5m"
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    today = entrymod.to_eastern(now).date().isoformat()
    key = (symbol, date, interval)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and (date != today or time.time() - hit[0] < 30):
        return hit[1]
    if CHART_DIR:
        text = entrymod.read_text(entrymod.offline_file(CHART_DIR, symbol))
    else:
        rng = "1d" if date == today else ("7d" if interval == "1m" else "60d")
        text = entrymod.yahoo_get(entrymod.chart_path(symbol, rng, interval))
    ch = entrymod.parse_chart(text, symbol)
    step = 60 if interval == "1m" else 300
    bars = []
    for b in ch["bars"]:
        if b["t"] % step:
            continue                       # the live tick is not a candle
        local = entrymod.to_eastern(datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc).replace(tzinfo=None))
        mins = local.hour * 60 + local.minute
        if local.date().isoformat() != date or not 570 <= mins < 960:
            continue
        bars.append({"time": calendar.timegm(local.timetuple()), "open": round(b["o"], 4), "high": round(b["h"], 4),
                     "low": round(b["l"], 4), "close": round(b["c"], 4), "volume": b["v"]})
    out = {"symbol": symbol, "date": date, "interval": interval, "bars": bars,
           "prev_close": ch.get("prev_close") if date == today else None}
    with _LOCK:
        _CACHE[key] = (time.time(), out)
    return out


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
        if p == "/api/export":
            import export
            data, man = export.build(ROOT)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="autopilot-export-%s.zip"'
                             % datetime.date.today().isoformat())
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/stats":
            return self._send(200, stats.report(AP))
        if p == "/api/chart":
            q = parse_qs(urlparse(self.path).query)
            try:
                return self._send(200, chart(q.get("symbol", [""])[0].upper(), q.get("date", [""])[0],
                                             q.get("interval", ["5m"])[0]))
            except Exception as e:
                return self._send(502, {"error": "chart data: %s" % str(e)[:200]})
        if p == "/tradechart.js":
            with open(os.path.join(HERE, "tradechart.js"), "rb") as fh:
                return self._send(200, fh.read(), "application/javascript; charset=utf-8")
        if p.startswith("/vendor/") and p.count("/") == 2 and p.endswith(".js"):
            f = os.path.join(HERE, "vendor", os.path.basename(p))
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), "application/javascript; charset=utf-8")
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
