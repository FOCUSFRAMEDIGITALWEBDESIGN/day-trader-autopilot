#!/usr/bin/env python3
"""Cloud entry point (Railway, any Docker host): the autopilot and the dashboard in one container.

- trader/ is the persistent volume (mount it at /app/trader): trades, settings, logs survive redeploys.
- First start: trader/config.json and trader/autopilot.json are seeded from config/.
- The dashboard listens on $PORT on all interfaces, so DASHBOARD_TOKEN is required.
- Both processes are restarted if they exit; SIGTERM (a redeploy) stops both cleanly.
Keys come from the service variables: ALPACA_API_KEY, ALPACA_SECRET_KEY (paper), DASHBOARD_TOKEN.
"""
import os, shutil, signal, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def log(msg):
    print("[run_all] " + msg, flush=True)


def seed():
    t = os.path.join(ROOT, "trader")
    os.makedirs(os.path.join(t, "autopilot"), exist_ok=True)
    for name in ("config.json", "autopilot.json"):
        dst = os.path.join(t, name)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(ROOT, "config", name), dst)
            log("seeded trader/%s from config/" % name)


def main():
    if not os.environ.get("DASHBOARD_TOKEN") or len(os.environ["DASHBOARD_TOKEN"]) < 16:
        sys.exit("[run_all] set DASHBOARD_TOKEN (at least 16 characters): the dashboard is public on the internet")
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")):
        log("WARNING: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set: the bot cannot trade until they are")
    if str(os.environ.get("ALPACA_PAPER_TRADE", "true")).lower() != "true":
        sys.exit("[run_all] ALPACA_PAPER_TRADE must be true: the autopilot trades paper accounts only")
    seed()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    port = os.environ.get("PORT", "8765")
    cmds = {"dashboard": [PY, os.path.join(ROOT, "dashboard", "server.py"), "--host", "0.0.0.0", "--port", port],
            "autopilot": [PY, os.path.join(ROOT, "autopilot", "autopilot.py"), "run"]}
    procs, backoff, stopping = {}, {k: 5 for k in cmds}, []

    def stop(*_):
        stopping.append(1)
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for name, cmd in cmds.items():
        procs[name] = subprocess.Popen(cmd, cwd=ROOT, env=env)
        log("started %s (pid %d)%s" % (name, procs[name].pid, " on port " + port if name == "dashboard" else ""))
    while not stopping:
        time.sleep(2)
        for name, p in list(procs.items()):
            code = p.poll()
            if code is not None and not stopping:
                log("%s exited with %s, restarting in %ds" % (name, code, backoff[name]))
                time.sleep(backoff[name])
                backoff[name] = min(backoff[name] * 2, 300)
                procs[name] = subprocess.Popen(cmds[name], cwd=ROOT, env=env)
    for p in procs.values():
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    log("stopped")


if __name__ == "__main__":
    main()
