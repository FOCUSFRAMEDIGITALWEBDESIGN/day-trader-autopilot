"""Download 60 days of 5-minute Yahoo bars for the tuning universe (polite: 1 request / 1.2 s)."""
import os, sys, time, urllib.request, gzip
HERE = os.path.dirname(os.path.abspath(__file__))
SYMS = open(os.path.join(HERE, "universe.txt")).read().split()
out = os.path.join(HERE, "data")
for s in SYMS:
    p = os.path.join(out, "%s-60d5m.json.gz" % s)
    if os.path.exists(p):
        continue
    for host in ("query2", "query1"):
        try:
            req = urllib.request.Request("https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=5m&range=60d" % (host, s),
                                         headers={"User-Agent": "day-trader-skill/2.0"})
            data = urllib.request.urlopen(req, timeout=30).read()
            with gzip.open(p, "wb") as f:
                f.write(data)
            print("ok", s, flush=True)
            break
        except Exception as e:
            print("fail", s, host, e, flush=True)
            time.sleep(6)
    time.sleep(1.2)
