"""Alpaca history -> the Yahoo chart format the pack reads, against the mock (pagination, SIP->IEX)."""
import datetime, gzip, json, os, shutil, socket, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
sys.path.insert(0, os.path.join(ROOT, "tuning"))
import mock_broker  # noqa: E402
import alpaca_data  # noqa: E402
import backtest as bt  # noqa: E402
from alpaca import Alpaca  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")


def yahoo_to_alpaca_bars(path):
    with gzip.open(path, "rt") as f:
        r = json.load(f)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r["timestamp"]):
        if q["close"][i] is None:
            continue
        out.append({"t": datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i]})
    return out


class AlpacaHistory(unittest.TestCase):
    def test_round_trip(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        srv = mock_broker.serve(port, {})
        d = tempfile.mkdtemp()
        try:
            mock_broker.S["bars"] = {"TSLA": yahoo_to_alpaca_bars(FIX)}
            mock_broker.S["deny_sip"] = True
            api = Alpaca("k", "s", base_url="http://127.0.0.1:%d" % port)
            api.page_limit = 1000                       # forces several pages
            old = alpaca_data.OUT
            alpaca_data.OUT = d
            msgs = []
            self.assertEqual(alpaca_data.fetch_history(["TSLA"], 1, msgs.append, api=api), 1)
            alpaca_data.OUT = old
            self.assertTrue(any("IEX" in m for m in msgs))
            ours = bt.sessions_of(os.path.join(d, "TSLA-alpaca5m.json.gz"))
            theirs = bt.sessions_of(FIX)
            self.assertEqual([x for x, _ in ours], [x for x, _ in theirs])
            self.assertEqual(ours[-1][1][10]["c"], theirs[-1][1][10]["c"])
        finally:
            srv.shutdown()
            mock_broker.S.pop("deny_sip", None)
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
