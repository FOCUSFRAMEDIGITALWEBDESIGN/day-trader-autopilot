"""Find the public trycloudflare addresses of this server's quick tunnels, and tell you when they change.

cloudflared serves its metrics on 127.0.0.1:20241-20245 and answers /quicktunnel with the tunnel's
hostname. The autopilot's own tunnel is the one whose /healthz answers "ok"; every other one (your
FOMO-Bot's) is reported as well. Best effort: if nothing answers, nothing happens.
"""
import json
import urllib.request

PORTS = range(20241, 20246)


def _get(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": "day-trader-autopilot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def hostnames(ports=PORTS, base="http://127.0.0.1"):
    out = []
    for p in ports:
        try:
            h = json.loads(_get("%s:%d/quicktunnel" % (base, p), 2) or "{}").get("hostname")
        except Exception:
            continue
        if h and h not in out:
            out.append(h)
    return out


def classify(hosts, scheme="https"):
    """-> (ours, others): ours is the host whose /healthz is the autopilot's 'ok'."""
    ours, others = None, []
    for h in hosts:
        try:
            ok = _get("%s://%s/healthz" % (scheme, h), 8).strip() == "ok"
        except Exception:
            ok = False
        if ok and not ours:
            ours = h
        else:
            others.append(h)
    return ours, others
