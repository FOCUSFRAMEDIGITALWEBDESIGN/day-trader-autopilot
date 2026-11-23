"""Alpaca REST client, PAPER ONLY.

Standard library only. The trading host is hard-wired to Alpaca's paper endpoint
and the account number must start with "PA" (Alpaca's paper accounts), so this
client cannot reach a live account even if someone pastes live keys.

Keys come from the environment (ALPACA_API_KEY / ALPACA_SECRET_KEY), or from the
file named by ALPACA_ENV_FILE / the project's .env. They are never printed.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class AlpacaError(Exception):
    pass


def _read_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_keys(root):
    env = dict(_read_env_file(os.environ.get("ALPACA_ENV_FILE") or os.path.join(root, ".env")))
    env.update({k: v for k, v in os.environ.items() if k.startswith("ALPACA_")})
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if str(env.get("ALPACA_PAPER_TRADE", "true")).lower() not in ("true", "1", "yes"):
        raise AlpacaError("ALPACA_PAPER_TRADE is not true. The autopilot trades paper accounts only.")
    return key, secret


class Alpaca:
    def __init__(self, key, secret, base_url=None, data_url=None, timeout=20):
        # base_url is only overridable for the local mock broker used by the tests
        self.base = (base_url or PAPER_URL).rstrip("/")
        self.data = (data_url or DATA_URL).rstrip("/")
        if self.base != PAPER_URL and not self.base.startswith("http://127.0.0.1"):
            raise AlpacaError("refusing %s: the autopilot only talks to %s" % (self.base, PAPER_URL))
        if not key or not secret:
            raise AlpacaError("no Alpaca paper keys: set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                              "(environment or the .env file next to the autopilot)")
        self._h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                   "Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "day-trader-autopilot/1.0"}
        self.timeout = timeout
        self._account_checked = False

    # -------------------------------------------------------------- http
    def _req(self, method, url, body=None, retries=3):
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers=self._h)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode() or "null"
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:400]
                if e.code == 429 or e.code >= 500:
                    last = "HTTP %d %s" % (e.code, msg)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if e.code == 404 and method == "GET":
                    return None
                raise AlpacaError("Alpaca %s %s: HTTP %d %s" % (method, url.split("?")[0].replace(self.base, ""),
                                                               e.code, msg))
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
        raise AlpacaError("Alpaca %s %s failed: %s" % (method, url.split("?")[0], last))

    def get(self, path, **q):
        q = {k: v for k, v in q.items() if v is not None}
        return self._req("GET", self.base + path + ("?" + urllib.parse.urlencode(q) if q else ""))

    # -------------------------------------------------------------- account
    def account(self):
        a = self.get("/v2/account")
        if not a:
            raise AlpacaError("Alpaca returned no account")
        num = str(a.get("account_number", ""))
        if self.base == PAPER_URL and not num.startswith("PA"):
            raise AlpacaError("account %s… does not look like a paper account (PA…): refusing to trade" % num[:3])
        self._account_checked = True
        return a

    def clock(self):
        return self.get("/v2/clock")

    def positions(self):
        return self.get("/v2/positions") or []

    def position(self, symbol):
        return self.get("/v2/positions/" + urllib.parse.quote(symbol))

    def orders(self, status="all", after=None, nested=True, limit=200):
        return self.get("/v2/orders", status=status, after=after, nested=str(nested).lower(), limit=limit,
                        direction="desc") or []

    def order(self, order_id):
        return self.get("/v2/orders/" + order_id, nested="true")

    def portfolio_history(self, period="1M", timeframe="1D"):
        return self.get("/v2/account/portfolio/history", period=period, timeframe=timeframe)

    def latest_trade(self, symbol):
        try:
            r = self._req("GET", "%s/v2/stocks/%s/trades/latest?feed=iex" % (self.data, urllib.parse.quote(symbol)),
                          retries=1)
            return (r or {}).get("trade", {}).get("p")
        except AlpacaError:
            return None

    # -------------------------------------------------------------- orders
    def submit(self, ticket, client_order_id=None):
        """ticket = the pack's place_stock_order input (flat strings). Mapped to the REST body."""
        if not self._account_checked:
            self.account()
        body = {"symbol": ticket["symbol"], "side": ticket["side"], "type": ticket.get("type", "market"),
                "time_in_force": ticket.get("time_in_force", "day")}
        if ticket.get("qty") is not None:
            body["qty"] = str(ticket["qty"])
        if ticket.get("notional") is not None:
            body["notional"] = str(ticket["notional"])
        if ticket.get("limit_price") is not None:
            body["limit_price"] = str(ticket["limit_price"])
        if ticket.get("take_profit_limit_price") or ticket.get("stop_loss_stop_price"):
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": str(ticket["take_profit_limit_price"])}
            sl = {"stop_price": str(ticket["stop_loss_stop_price"])}
            if ticket.get("stop_loss_limit_price"):
                sl["limit_price"] = str(ticket["stop_loss_limit_price"])
            body["stop_loss"] = sl
        if client_order_id:
            body["client_order_id"] = client_order_id[:48]
        return self._req("POST", self.base + "/v2/orders", body, retries=1)

    def cancel(self, order_id):
        try:
            return self._req("DELETE", self.base + "/v2/orders/" + order_id, retries=1)
        except AlpacaError as e:
            if "422" in str(e) or "404" in str(e):
                return None  # already filled or canceled
            raise

    def wait_fill(self, order_id, seconds=20):
        end = time.time() + seconds
        o = None
        while time.time() < end:
            o = self.order(order_id)
            if o and o.get("status") in ("filled", "canceled", "rejected", "expired"):
                return o
            time.sleep(1)
        return o
