#!/usr/bin/env python3
"""Telegram push messages: trades, exits, errors, restarts and the daily summary.

    python autopilot/notify.py setup BOT_TOKEN   # finds your chat id, writes both into .env, sends a test
    python autopilot/notify.py test               # one test message

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (in .env or the environment). Without them every
send() is a quiet no-op, so the bot works the same with or without Telegram.
Sending never blocks trading: it runs in a background thread with a short timeout, and the same
error is sent at most once every 30 minutes.
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
_last = {}
_lock = threading.Lock()


def _env():
    vals = {}
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID")
    return tok, chat


def enabled():
    tok, chat = _env()
    return bool(tok and chat)


def _post(tok, method, params, timeout=10):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request("%s/bot%s/%s" % (API, tok, method), data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def send(text, key=None, every=1800, wait=False):
    """Send `text`. `key`: messages with the same key go out at most once per `every` seconds."""
    tok, chat = _env()
    if not (tok and chat):
        return False
    k = key or text
    with _lock:
        if key and time.time() - _last.get(k, 0) < every:
            return False
        _last[k] = time.time()

    def go():
        try:
            _post(tok, "sendMessage", {"chat_id": chat, "text": text[:3900], "disable_web_page_preview": "true"})
        except Exception as e:  # never let a message break trading
            sys.stderr.write("telegram: %s\n" % str(e)[:200])
    t = threading.Thread(target=go, daemon=not wait)
    t.start()
    if wait:
        t.join(15)
    return True


def setup(token, seconds=120):
    """Wait for a message to the bot, take its chat id, write token and chat id into .env, test."""
    print("Schreib deinem Bot jetzt in Telegram irgendeine Nachricht (z. B. /start). Ich warte bis zu %d s ..." % seconds)
    end, chat = time.time() + seconds, None
    while time.time() < end and not chat:
        try:
            r = _post(token, "getUpdates", {"timeout": 10}, timeout=20)
        except Exception as e:
            print("Telegram antwortet nicht: %s" % e)
            return 1
        if not r.get("ok"):
            print("Der Bot-Token stimmt nicht: %s" % r.get("description"))
            return 1
        for u in r.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id"):
                chat = str(msg["chat"]["id"])
        if not chat:
            time.sleep(2)
    if not chat:
        print("Keine Nachricht angekommen. Starte das Setup noch einmal und schreib dem Bot.")
        return 1
    path = os.path.join(ROOT, ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="))]
    lines += ["TELEGRAM_BOT_TOKEN=" + token, "TELEGRAM_CHAT_ID=" + chat]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = token, chat
    send("✅ Day Trader Autopilot ist verbunden. Ab jetzt kommen hier Trades, Ausstiege, Fehler und die Tageszusammenfassung.",
         wait=True)
    print("Fertig: Chat %s gespeichert, Testnachricht gesendet. Starte den Bot neu (stop.bat, start.bat)." % chat)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "setup":
        sys.exit(setup(sys.argv[2].strip()))
    if len(sys.argv) >= 2 and sys.argv[1] == "test":
        ok = send("🔔 Test vom Day Trader Autopilot.", wait=True)
        print("gesendet" if ok else "Telegram ist nicht eingerichtet (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen)")
        sys.exit(0 if ok else 1)
    print(__doc__)
