"""A tiny local stand-in for Alpaca's paper REST API, for offline tests and replays.

    python3 autopilot/mock_broker.py --port 8799 --price TSLA=364.30

Market orders fill at once at the --price given for the symbol. Bracket legs wait
("new" / "held") until POST /mock/fill_leg {"symbol": "TSLA", "type": "limit"|"stop"}.
"""
import argparse, json, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

S = {"cash": 100000.0, "orders": {}, "positions": {}, "prices": {}}
LOCK = threading.Lock()


def _pos_fill(sym, side, qty, price):
    """Signed positions: a short is a negative qty with a negative cost basis."""
    p = S["positions"].setdefault(sym, {"qty": 0.0, "cost": 0.0})
    d = qty if side == "buy" else -qty
    opening = p["qty"] == 0 or (p["qty"] > 0) == (d > 0)
    if opening:
        p["cost"] += d * price
    else:
        avg = p["cost"] / p["qty"]
        p["cost"] += d * avg
    p["qty"] += d
    S["cash"] -= d * price
    if abs(p["qty"]) <= 1e-9:
        S["positions"].pop(sym, None)


def _cancel_family(o):
    """OCO semantics: when one exit of a pair fills or is canceled, the other is canceled."""
    root = o.get("parent") or o["id"]
    for x in S["orders"].values():
        if x["id"] != o["id"] and x["status"] in ("new", "held") and (
                x.get("parent") == root or (x["id"] == root and x.get("order_class") == "oco")):
            x["status"] = "canceled"


def _order_view(o):
    v = dict(o)
    v["legs"] = [S["orders"][i] for i in o.get("leg_ids", [])]
    v.pop("leg_ids", None)
    return v


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        with LOCK:
            if path == "/v2/account":
                eq = S["cash"] + sum(p["qty"] * S["prices"].get(s, 0) for s, p in S["positions"].items())
                return self._send(200, {"account_number": "PA0MOCK0001", "status": "ACTIVE", "cash": str(round(S["cash"], 2)),
                                        "equity": str(round(eq, 2)), "last_equity": "100000", "buying_power": str(S["cash"]),
                                        "currency": "USD", "daytrade_count": 0, "pattern_day_trader": False})
            if path == "/v2/clock":
                return self._send(200, {"is_open": True})
            if path == "/v2/positions":
                return self._send(200, [self._pos(s) for s in S["positions"]])
            if path.startswith("/v2/positions/"):
                s = path.rsplit("/", 1)[1]
                return self._send(200, self._pos(s)) if s in S["positions"] else self._send(404, {"message": "no position"})
            if path == "/v2/orders":
                return self._send(200, [_order_view(o) for o in S["orders"].values() if not o.get("parent")])
            if path.startswith("/v2/orders/"):
                o = S["orders"].get(path.rsplit("/", 1)[1])
                return self._send(200, _order_view(o)) if o else self._send(404, {"message": "not found"})
            if path == "/v2/stocks/snapshots":
                q = parse_qs(urlparse(self.path).query)
                want = q.get("symbols", [""])[0].split(",")
                return self._send(200, {s: S["snapshots"][s] for s in want if s in S.get("snapshots", {})})
            if path == "/v2/stocks/bars":
                q = parse_qs(urlparse(self.path).query)
                want = q.get("symbols", [""])[0].split(",")
                return self._send(200, {"bars": {s: S["daily"][s] for s in want if s in S.get("daily", {})},
                                        "next_page_token": None})
            if path.startswith("/v2/stocks/") and path.endswith("/bars"):
                q = parse_qs(urlparse(self.path).query)
                if q.get("feed", [""])[0] == "sip" and S.get("deny_sip"):
                    return self._send(403, {"message": "subscription does not permit querying recent SIP data"})
                bars = S.get("bars", {}).get(path.split("/")[3], [])
                start = int(q.get("page_token", ["0"])[0])
                lim = int(q.get("limit", ["10000"])[0])
                page = bars[start:start + lim]
                nxt = str(start + lim) if start + lim < len(bars) else None
                return self._send(200, {"bars": page, "next_page_token": nxt})
            if path.startswith("/v2/stocks/") and path.endswith("/trades/latest"):
                sym = path.split("/")[3]
                return self._send(200, {"symbol": sym, "trade": {"p": S["prices"].get(sym)}})
            if path == "/mock/state":
                return self._send(200, {k: v for k, v in S.items()})
        self._send(404, {"message": "unknown"})

    def _pos(self, s):
        p = S["positions"][s]
        px = S["prices"].get(s, p["cost"] / p["qty"])
        return {"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(round(p["cost"] / p["qty"], 4)),
                "current_price": str(px), "market_value": str(round(px * p["qty"], 2)), "side": "long",
                "unrealized_pl": str(round(px * p["qty"] - p["cost"], 2)),
                "unrealized_plpc": str(round((px * p["qty"] - p["cost"]) / p["cost"], 5))}

    def do_POST(self):
        path = urlparse(self.path).path
        b = self._body()
        with LOCK:
            if path == "/v2/orders":
                sym, qty, side = b["symbol"], float(b["qty"]), b["side"]
                held_now = S["positions"].get(sym, {}).get("qty", 0)
                short_entry = side == "sell" and b.get("order_class") == "bracket" and held_now <= 0
                if side == "sell" and held_now < qty and not short_entry:
                    return self._send(403, {"message": "insufficient qty available for order"})
                held = sum(float(o["qty"]) for o in S["orders"].values() if o["symbol"] == sym and o["side"] == "sell"
                           and o["type"] == "limit" and o["status"] in ("new", "held"))
                if side == "sell" and not short_entry and held + qty > held_now + 1e-9:
                    return self._send(403, {"message": "insufficient qty available for order (held for orders)"})
                if b.get("order_class") == "oco":
                    oid, lid = str(uuid.uuid4()), str(uuid.uuid4())
                    S["orders"][lid] = {"id": lid, "parent": oid, "symbol": sym, "side": side, "qty": b["qty"], "type": "stop",
                                        "status": "held", "stop_price": b["stop_loss"]["stop_price"], "filled_qty": "0",
                                        "filled_avg_price": None}
                    o = {"id": oid, "symbol": sym, "side": side, "qty": b["qty"], "type": "limit", "order_class": "oco",
                         "status": "new", "limit_price": b["take_profit"]["limit_price"], "filled_qty": "0",
                         "filled_avg_price": None, "leg_ids": [lid]}
                    S["orders"][oid] = o
                    return self._send(200, _order_view(o))
                px = S["prices"].get(sym)
                oid = str(uuid.uuid4())
                o = {"id": oid, "client_order_id": b.get("client_order_id"), "symbol": sym, "side": side, "qty": b["qty"],
                     "type": b.get("type"), "order_class": b.get("order_class", "simple"), "status": "filled",
                     "filled_qty": b["qty"], "filled_avg_price": str(px), "leg_ids": []}
                _pos_fill(sym, side, qty, px)
                if b.get("order_class") == "bracket":
                    for typ, price in (("limit", b["take_profit"]["limit_price"]), ("stop", b["stop_loss"]["stop_price"])):
                        lid = str(uuid.uuid4())
                        S["orders"][lid] = {"id": lid, "parent": oid, "symbol": sym, "side": "sell" if side == "buy" else "buy",
                                            "qty": b["qty"],
                                            "type": typ, "status": "new" if typ == "limit" else "held",
                                            "limit_price" if typ == "limit" else "stop_price": price,
                                            "filled_qty": "0", "filled_avg_price": None}
                        o["leg_ids"].append(lid)
                S["orders"][oid] = o
                return self._send(200, _order_view(o))
            if path == "/mock/fill_leg":
                for o in S["orders"].values():
                    is_exit = o.get("parent") or o.get("order_class") == "oco"
                    if is_exit and o["symbol"] == b["symbol"] and o["type"] == b["type"] and o["status"] in ("new", "held"):
                        px = float(o.get("limit_price") or o.get("stop_price"))
                        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(px))
                        _pos_fill(o["symbol"], o["side"], float(o["qty"]), px)
                        _cancel_family(o)
                        return self._send(200, o)
                return self._send(404, {"message": "no open leg"})
            if path == "/mock/price":
                S["prices"].update({k: float(v) for k, v in b.items()})
                return self._send(200, S["prices"])
        self._send(404, {"message": "unknown"})

    def do_PATCH(self):
        path = urlparse(self.path).path
        b = self._body()
        with LOCK:
            o = S["orders"].get(path.rsplit("/", 1)[1])
            if not o or o["status"] not in ("new", "held"):
                return self._send(422, {"message": "order is not replaceable"})
            for k in ("stop_price", "limit_price", "qty"):
                if k in b:
                    o[k] = b[k]
            return self._send(200, o)

    def do_DELETE(self):
        path = urlparse(self.path).path
        with LOCK:
            o = S["orders"].get(path.rsplit("/", 1)[1])
            if not o:
                return self._send(404, {"message": "not found"})
            if o["status"] in ("filled", "canceled"):
                return self._send(422, {"message": "order is not cancelable"})
            o["status"] = "canceled"
            if o.get("parent") or o.get("order_class") == "oco":
                _cancel_family(o)
            self.send_response(204)
            self.end_headers()


def serve(port, prices):
    S["prices"].update(prices)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--price", action="append", default=[])
    a = ap.parse_args()
    serve(a.port, {k: float(v) for k, v in (x.split("=") for x in a.price)})
    print("mock Alpaca on http://127.0.0.1:%d" % a.port)
    threading.Event().wait()
