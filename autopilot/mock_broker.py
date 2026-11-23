"""A tiny local stand-in for Alpaca's paper REST API, for offline tests and replays.

    python3 autopilot/mock_broker.py --port 8799 --price TSLA=364.30

Market orders fill at once at the --price given for the symbol. Bracket legs wait
("new" / "held") until POST /mock/fill_leg {"symbol": "TSLA", "type": "limit"|"stop"}.
"""
import argparse, json, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

S = {"cash": 100000.0, "orders": {}, "positions": {}, "prices": {}}
LOCK = threading.Lock()


def _pos_fill(sym, side, qty, price):
    p = S["positions"].setdefault(sym, {"qty": 0.0, "cost": 0.0})
    if side == "buy":
        p["qty"] += qty
        p["cost"] += qty * price
        S["cash"] -= qty * price
    else:
        avg = p["cost"] / p["qty"] if p["qty"] else price
        p["qty"] -= qty
        p["cost"] -= avg * qty
        S["cash"] += qty * price
    if p["qty"] <= 1e-9:
        S["positions"].pop(sym, None)


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
                if side == "sell" and S["positions"].get(sym, {}).get("qty", 0) < qty:
                    return self._send(403, {"message": "insufficient qty available for order"})
                held = sum(float(S["orders"][l]["qty"]) for o in S["orders"].values() if o["symbol"] == sym
                           for l in o.get("leg_ids", []) if S["orders"][l]["status"] in ("new", "held") and S["orders"][l]["type"] == "limit")
                if side == "sell" and held:
                    return self._send(403, {"message": "insufficient qty available for order (held for orders)"})
                px = S["prices"].get(sym)
                oid = str(uuid.uuid4())
                o = {"id": oid, "client_order_id": b.get("client_order_id"), "symbol": sym, "side": side, "qty": b["qty"],
                     "type": b.get("type"), "order_class": b.get("order_class", "simple"), "status": "filled",
                     "filled_qty": b["qty"], "filled_avg_price": str(px), "leg_ids": []}
                _pos_fill(sym, side, qty, px)
                if b.get("order_class") == "bracket":
                    for typ, price in (("limit", b["take_profit"]["limit_price"]), ("stop", b["stop_loss"]["stop_price"])):
                        lid = str(uuid.uuid4())
                        S["orders"][lid] = {"id": lid, "parent": oid, "symbol": sym, "side": "sell", "qty": b["qty"],
                                            "type": typ, "status": "new" if typ == "limit" else "held",
                                            "limit_price" if typ == "limit" else "stop_price": price,
                                            "filled_qty": "0", "filled_avg_price": None}
                        o["leg_ids"].append(lid)
                S["orders"][oid] = o
                return self._send(200, _order_view(o))
            if path == "/mock/fill_leg":
                for o in S["orders"].values():
                    if o.get("parent") and o["symbol"] == b["symbol"] and o["type"] == b["type"] and o["status"] in ("new", "held"):
                        px = float(o.get("limit_price") or o.get("stop_price"))
                        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(px))
                        _pos_fill(o["symbol"], "sell", float(o["qty"]), px)
                        for sib in S["orders"].values():
                            if sib.get("parent") == o["parent"] and sib["id"] != o["id"]:
                                sib["status"] = "canceled"
                        return self._send(200, o)
                return self._send(404, {"message": "no open leg"})
            if path == "/mock/price":
                S["prices"].update({k: float(v) for k, v in b.items()})
                return self._send(200, S["prices"])
        self._send(404, {"message": "unknown"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        with LOCK:
            o = S["orders"].get(path.rsplit("/", 1)[1])
            if not o:
                return self._send(404, {"message": "not found"})
            if o["status"] in ("filled", "canceled"):
                return self._send(422, {"message": "order is not cancelable"})
            o["status"] = "canceled"
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
