#!/usr/bin/env python3
"""Supervisor: the autopilot and the dashboard in one place, restarted if they stop.

    python deploy/run_all.py --local     # your PC (start.bat): dashboard on 127.0.0.1, keeps Windows awake
    python deploy/run_all.py             # a cloud container (Railway): dashboard on 0.0.0.0:$PORT, token required

- trader/config.json and trader/autopilot.json are seeded from config/ on the first start.
- .env next to the project is read (Alpaca keys, Telegram), the environment wins.
- --local: Windows is kept awake (not the screen) on trading days from 9:00 to 16:15 New York time,
  so a sleeping PC cannot leave a trade unmanaged. Only one copy runs at a time (a lock file).
- A child that stops is restarted (5 s, then longer), and Telegram is told.
"""
import argparse, datetime, os, shutil, signal, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
PY = sys.executable
AP = os.path.join(ROOT, "trader", "autopilot")


def log(msg):
    print("[run_all %s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def seed():
    t = os.path.join(ROOT, "trader")
    os.makedirs(os.path.join(t, "autopilot"), exist_ok=True)
    for name in ("config.json", "autopilot.json"):
        dst = os.path.join(t, name)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(ROOT, "config", name), dst)
            log("seeded trader/%s from config/" % name)


def load_env_file():
    from alpaca import _read_env_file
    for k, v in _read_env_file(os.path.join(ROOT, ".env")).items():
        if v and not os.environ.get(k):
            os.environ[k] = v


# ---------------------------------------------------------------- one copy only
class SingleInstance:
    def __init__(self, path):
        self.path, self.fh = path, None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True


# ---------------------------------------------------------------- keep Windows awake
def market_hours_now():
    """True on NYSE trading days from 9:00 to 16:15 New York time (the pack's own calendar)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("rl_alerts", os.path.join(ROOT, "trader-alerts", "alerts.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    now = m.to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
    ok, _ = m.trading_day(now.date())
    return ok and 9 * 60 <= now.hour * 60 + now.minute <= 16 * 60 + 15


class KeepAwake:
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self):
        self.on = False

    def set(self, want):
        if os.name != "nt" or want == self.on:
            return
        import ctypes
        flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if want else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        self.on = want
        log("Windows %s" % ("is kept awake (US market hours)" if want else "may sleep again"))


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--local", action="store_true", help="your PC: dashboard on 127.0.0.1, keep Windows awake")
    a = a.parse_args()
    load_env_file()
    if not a.local and (not os.environ.get("DASHBOARD_TOKEN") or len(os.environ["DASHBOARD_TOKEN"]) < 16):
        sys.exit("[run_all] set DASHBOARD_TOKEN (at least 16 characters): the dashboard is public on the internet")
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")):
        log("WARNING: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set: the bot cannot trade until they are")
    if str(os.environ.get("ALPACA_PAPER_TRADE", "true")).lower() != "true":
        sys.exit("[run_all] ALPACA_PAPER_TRADE must be true: the autopilot trades paper accounts only")
    seed()
    lock = SingleInstance(os.path.join(AP, "run_all.lock"))
    if not lock.acquire():
        sys.exit("[run_all] the autopilot is already running (trader/autopilot/run_all.lock): stop it first")
    import notify
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    host = "127.0.0.1" if a.local else (os.environ.get("DASHBOARD_HOST") or "0.0.0.0")
    port = os.environ.get("DASHBOARD_PORT" if a.local else "PORT") or os.environ.get("DASHBOARD_PORT") or "8765"
    logs = {}
    cmds = {"dashboard": [PY, os.path.join(ROOT, "dashboard", "server.py"), "--host", host, "--port", port],
            "autopilot": [PY, os.path.join(ROOT, "autopilot", "autopilot.py"), "run"]}
    if notify.enabled():   # Telegram control: only when token and chat id are set
        cmds["telegram"] = [PY, os.path.join(ROOT, "autopilot", "telegram_bot.py")]
    procs, backoff, stopping, started = {}, {k: 5 for k in cmds}, [], {}
    awake = KeepAwake()

    def spawn(name):
        out = None
        if a.local:   # on the PC each child writes its own log file
            logs.setdefault(name, open(os.path.join(AP, name + ".log"), "a", encoding="utf-8"))
            out = logs[name]
        procs[name] = subprocess.Popen(cmds[name], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT if out else None)
        started[name] = time.time()

    def stop(*_):
        stopping.append(1)
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for name in cmds:
        spawn(name)
        log("started %s (pid %d)%s" % (name, procs[name].pid,
                                         " on http://%s:%s" % (host, port) if name == "dashboard" else ""))
    notify.send("🟢 Day Trader Autopilot gestartet (Paper). Befehle: /hilfe")
    last_awake_check = 0
    while not stopping:
        time.sleep(2)
        if a.local and time.time() - last_awake_check > 60:
            last_awake_check = time.time()
            try:
                awake.set(market_hours_now())
            except Exception as e:
                log("keep-awake check failed: %s" % e)
        for name, p in list(procs.items()):
            code = p.poll()
            if code is not None and not stopping:
                if time.time() - started[name] > 600:
                    backoff[name] = 5          # it ran a while: start the backoff over
                log("%s exited with %s, restarting in %ds" % (name, code, backoff[name]))
                notify.send("⚠️ %s ist abgestürzt (Code %s) und wird in %d s neu gestartet." % (
                    {"autopilot": "Der Autopilot", "dashboard": "Das Dashboard"}.get(name, "Der Telegram-Bot"), code, backoff[name]), key="crash-" + name)
                time.sleep(backoff[name])
                backoff[name] = min(backoff[name] * 2, 300)
                spawn(name)
    awake.set(False)
    for p in procs.values():
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    notify.send("🔴 Day Trader Autopilot gestoppt.", wait=True)
    log("stopped")


if __name__ == "__main__":
    main()
