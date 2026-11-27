#!/usr/bin/env python3
"""Control the autopilot from Telegram.

Runs next to the autopilot (deploy/run_all.py starts it when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
are set). It answers ONLY your own chat (TELEGRAM_CHAT_ID); messages from anyone else are ignored.

  /status       phase, simulated account, today's P&L, open positions
  /positionen   every open position with stop, target and P&L
  /trades       today's trades
  /kandidaten   the last setups and why they were taken or skipped
  /statistik    all paper trades so far (the same numbers as the dashboard)
  /lernen       the last weekly learning run
  /pause        no new entries (open trades are still managed)
  /weiter       allow new entries again
  /flat         close every open position now (asks once more: /flat ja)
  /export       the weekly export file, as a document in this chat
  /link         the dashboard address
  /konto 2500   scale every limit to a new account size
  /hilfe        this list
Every Friday after the US close the export is sent by itself.
"""
import datetime
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
AP = os.path.join(ROOT, "trader", "autopilot")
import notify  # noqa: E402

COMMANDS = [("status", "Lage jetzt: Konto, Tages-P&L, offene Positionen"),
            ("positionen", "Offene Positionen mit Stop und Ziel"),
            ("trades", "Trades von heute"), ("kandidaten", "Letzte Setups und Gründe"),
            ("statistik", "Alle Paper-Trades ausgewertet"), ("vergleich", "Alle Taktiken im Vergleich"),
            ("lernen", "Letzter Lernlauf"),
            ("pause", "Keine neuen Einstiege"), ("weiter", "Einstiege wieder erlauben"),
            ("flat", "Alles sofort glattstellen"), ("export", "Wochen-Datei schicken"),
            ("link", "Dashboard-Adresse"), ("konto", "Kontogröße ändern, z. B. /konto 2500"),
            ("hilfe", "Alle Befehle")]


def rj(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def usd(v, sign=False):
    if v is None:
        return "–"
    s = ("+" if sign and v > 0 else "-" if v < 0 else "") + "$" + format(abs(float(v)), ",.2f")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------- telegram http
class Telegram:
    def __init__(self, token, chat, api=None):
        self.token, self.chat = token, str(chat)
        self.api = (api or os.environ.get("TELEGRAM_API") or "https://api.telegram.org").rstrip("/")
        self.offset = None

    def call(self, method, params=None, timeout=40):
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request("%s/bot%s/%s" % (self.api, self.token, method), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")

    KEYBOARD = json.dumps({"keyboard": [["/status", "/positionen"], ["/trades", "/vergleich"],
                                        ["/statistik", "/kandidaten"], ["/pause", "/weiter"], ["/export", "/hilfe"]],
                           "resize_keyboard": True, "is_persistent": True})

    def send(self, text):
        for i in range(0, len(text), 3900):
            self.call("sendMessage", {"chat_id": self.chat, "text": text[i:i + 3900],
                                      "disable_web_page_preview": "true", "reply_markup": self.KEYBOARD}, timeout=20)

    def send_document(self, filename, data, caption=""):
        b = uuid.uuid4().hex
        parts = []
        for k, v in (("chat_id", self.chat), ("caption", caption)):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode())
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
                      "Content-Type: application/zip\r\n\r\n" % (b, filename)).encode() + data + b"\r\n")
        body = b"".join(parts) + ("--%s--\r\n" % b).encode()
        req = urllib.request.Request("%s/bot%s/sendDocument" % (self.api, self.token), data=body, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + b})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    def updates(self, timeout=25):
        params = {"timeout": timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        r = self.call("getUpdates", params, timeout=timeout + 15)
        out = []
        for u in r.get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            out.append((str((msg.get("chat") or {}).get("id")), (msg.get("text") or "").strip()))
        return out


# ---------------------------------------------------------------- answers
def status_text():
    s = rj(os.path.join(AP, "status.json"), {}) or {}
    if not s:
        return "Noch kein Status: läuft der Autopilot?"
    d = s.get("day") or {}
    phase = {"trading": "handelt", "flatten": "stellt glatt", "premarket": "vor Börsenstart",
             "after close": "nach Börsenschluss", "closed": "Börse geschlossen"}.get(s.get("phase"), s.get("phase"))
    upd = s.get("updated_utc")
    age = None
    if upd:
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(
                upd.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            pass
    lines = ["📊 Status %s (New York)" % (s.get("updated") or "")[11:16],
             "Phase: %s%s" % (phase, " · ⏸ PAUSIERT" if s.get("paused") else "")]
    if age is not None and age > 300:
        lines.append("⚠️ Letzte Meldung des Bots vor %d min: läuft er noch?" % (age // 60))
    if s.get("sim_equity"):
        lines.append("Simuliertes Konto: %s (Start %s)" % (usd(s["sim_equity"]), usd(s.get("start_capital"))))
    lines.append("Heute realisiert: %s" % usd(d.get("realized", 0), True))
    trades = d.get("trades") or []
    open_t = [t for t in trades if t.get("status") in ("open", "pending")]
    lines.append("Trades heute: %d · offen: %d" % (len([t for t in trades if t.get("status") not in ("rejected", "missed")]),
                                                    len(open_t)))
    for t in open_t:
        lines.append(pos_line(t))
    if s.get("last_error"):
        lines.append("⚠️ " + s["last_error"][:200])
    return "\n".join(lines)


def pos_line(t):
    p = t.get("plan") or {}
    side = "Short" if t.get("side") == "sell" else "Long"
    sg = -1 if t.get("side") == "sell" else 1
    fill, last = t.get("fill_price") or p.get("entry"), t.get("last")
    pnl = sg * (last - fill) * (t.get("qty_open") or t.get("qty") or 0) if last and fill else None
    def px(v):
        return "–" if v is None else ("%.2f" % float(v)).replace(".", ",")
    return "• %s %s %s Stk @ %s · Stop %s%s · Ziel %s%s" % (
        t.get("symbol"), side, int(t.get("qty_open") or t.get("qty") or 0), px(fill), px(t.get("stop_now") or p.get("stop")),
        " (nachgezogen)" if t.get("stop_now") else "", "Trailing" if (t.get("exit_mode") or "").startswith("trail")
        else px(p.get("target")), (" · " + usd(pnl, True)) if pnl is not None else "")


def positions_text():
    d = (rj(os.path.join(AP, "status.json"), {}) or {}).get("day") or {}
    open_t = [t for t in d.get("trades") or [] if t.get("status") in ("open", "pending")]
    return ("📈 Offene Positionen\n" + "\n".join(pos_line(t) for t in open_t)) if open_t else "Keine offene Position."


def trades_text():
    d = (rj(os.path.join(AP, "status.json"), {}) or {}).get("day") or {}
    rows = [t for t in d.get("trades") or [] if t.get("status") not in ("rejected",)]
    if not rows:
        return "Heute noch keine Trades."
    how = {"target": "Ziel", "stop": "Stop", "trail": "Trailing-Stop", "flat": "Flat", "open": "offen",
           "pending": "wartet auf Ausführung", "missed": "verpasst (Limit)"}
    out = ["🧾 Trades heute"]
    for t in rows:
        out.append("%s %s %s · %s · %s%s" % (t.get("at"), t.get("symbol"), "Short" if t.get("side") == "sell" else "Long",
                                            how.get(t.get("status"), t.get("status")),
                                            usd(t["pnl"], True) if t.get("pnl") is not None else "–",
                                            (" (%+.2f R)" % t["R"]) if t.get("R") is not None else ""))
    return "\n".join(out)


def candidates_text(n=8):
    d = (rj(os.path.join(AP, "status.json"), {}) or {}).get("day") or {}
    cs = (d.get("candidates") or [])[:n]
    if not cs:
        return "Heute noch keine Kandidaten."
    out = ["🔎 Letzte Kandidaten"]
    for c in cs:
        res = "✅ Order" if c.get("result") == "ORDER PLACED" else "✗ " + (c.get("why") or c.get("result") or "läuft")[:110]
        out.append("%s %s%s: %s" % (c.get("first_seen"), c.get("symbol"), " (Short)" if c.get("side") == "sell" else "", res))
    return "\n".join(out)


def stats_text():
    import stats
    r = stats.report(AP)
    o = r["overall"]
    if not o.get("n"):
        return "Noch keine geschlossenen Trades."
    out = ["📊 Statistik (%d Trades, %d Tage)" % (o["n"], len(r["days"])),
           "Trefferquote %s %% · Ø %+.2f R · Summe %s" % (o["win_pct"], o["avg_R"] or 0, usd(o["pnl"], True)),
           "Profit-Faktor %s · max. Rückgang %s" % (o.get("profit_factor") or "–", usd(o.get("max_drawdown")))]
    if r.get("vs_backtest") is not None:
        out.append("Live vs. Backtest: %+.2f R" % r["vs_backtest"])
    for g in r["groups"]:
        if g["title"] in ("Richtung", "Setup", "Ausstiegsart", "Uhrzeit des Einstiegs (New York)"):
            out.append("\n" + g["title"] + ":")
            for row in g["rows"]:
                out.append("  %s: %d · Ø %+.2f R · %s%s" % (row["label"], row["n"], row["avg_R"] or 0,
                                                           usd(row["pnl"], True), "" if row["enough"] else " (wenige)"))
    return "\n".join(out)


def compare_text():
    import tactics
    doc = rj(os.path.join(AP, "shadow.json"), {}) or {}
    days = doc.get("days") or {}
    if not days:
        return "Noch kein Taktik-Vergleich. Er läuft jeden Handelstag nach US-Börsenschluss (ca. 22:20 Uhr)."
    allres = {}
    for day in days.values():
        for name, trades in day.items():
            allres.setdefault(name, []).extend(trades)
    rows = tactics.leaderboard(allres)
    out = ["🏁 Taktik-Vergleich: %d Handelstage, gleiche Kurse, ohne Orders" % len(days),
           "($ bei 1 %% Risiko = $20 pro R)"]
    for i, r in enumerate(rows, 1):
        if not r.get("n"):
            out.append("%d. %s: noch kein Signal" % (i, r["name"]))
            continue
        out.append("%d. %s: %d Trades · %s %% Treffer · Ø %+.2f R · Σ %+.1f R (%s)" % (
            i, r["name"], r["n"], r["win_pct"], r["avg_R"], r["sum_R"], usd(r["usd"], True)))
    if len(days) < 20:
        out.append("\nNoch wenige Tage: erst ab etwa 20 Handelstagen sagt die Reihenfolge etwas aus.")
    return "\n".join(out)


def learning_text():
    L = rj(os.path.join(AP, "learning.json"), []) or []
    if not L:
        return "Noch kein Lernlauf. Er startet automatisch nach Börsenschluss, einmal pro Woche."
    x = L[-1]
    ce = x.get("current_eval") or {}
    return "🧠 Lernen %s\nErgebnis: %s\nAktueller Plan im Test: %s Trades, Ø %s R\n%s" % (
        x.get("run_at"), x.get("result"), ce.get("n"), ce.get("mean"), x.get("current", ""))


def link_text():
    t = rj(os.path.join(AP, "tunnels.json"), {}) or {}
    tok = os.environ.get("DASHBOARD_TOKEN", "")
    if t.get("ours"):
        return "🔗 Dashboard: https://%s/?token=%s" % (t["ours"], tok)
    return "Kein Tunnel gefunden. Das Dashboard läuft intern auf Port %s." % (os.environ.get("PORT") or "8765")


# ---------------------------------------------------------------- the loop
class Bot:
    def __init__(self, tg):
        self.tg = tg
        self.flat_asked = 0

    def handle(self, text):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0].lstrip("/")
        arg = arg.strip()
        if cmd in ("start", "hilfe", "help"):
            return "\n".join(["🤖 Day Trader Autopilot (Paper)"] + ["/%s – %s" % c for c in COMMANDS])
        if cmd == "status":
            return status_text()
        if cmd == "positionen":
            return positions_text()
        if cmd == "trades":
            return trades_text()
        if cmd == "kandidaten":
            return candidates_text()
        if cmd == "statistik":
            return stats_text()
        if cmd == "lernen":
            return learning_text()
        if cmd == "vergleich":
            return compare_text()
        if cmd == "link":
            return link_text()
        if cmd == "pause":
            os.makedirs(AP, exist_ok=True)
            with open(os.path.join(AP, "PAUSE"), "w") as f:
                f.write("paused from Telegram\n")
            return "⏸ Pausiert: keine neuen Einstiege. Offene Trades werden weiter verwaltet. /weiter zum Fortsetzen."
        if cmd == "weiter":
            try:
                os.remove(os.path.join(AP, "PAUSE"))
            except OSError:
                pass
            return "▶️ Neue Einstiege sind wieder erlaubt."
        if cmd == "flat":
            if arg.lower() == "ja" and time.time() - self.flat_asked < 120:
                with open(os.path.join(AP, "FLATTEN_NOW"), "w") as f:
                    f.write("from Telegram\n")
                with open(os.path.join(AP, "PAUSE"), "w") as f:
                    f.write("paused after /flat from Telegram\n")
                return ("🛑 Wird glattgestellt: alle offenen Positionen werden beim nächsten Durchlauf (unter einer "
                        "Minute) geschlossen, neue Einstiege sind pausiert. /weiter hebt die Pause auf.")
            self.flat_asked = time.time()
            return "Wirklich ALLE offenen Positionen jetzt schließen? Antworte innerhalb von 2 Minuten mit: /flat ja"
        if cmd == "export":
            import export
            data, man = export.build(ROOT)
            self.tg.send_document("autopilot-export-%s.zip" % datetime.date.today().isoformat(), data,
                                  "Wochen-Export: %d Handelstage. Ohne Schlüssel und persönliche Daten." % len(man["days"]))
            return None
        if cmd == "konto":
            try:
                cap = float(arg.replace(".", "").replace(",", "."))
                import account_profile
                lim = account_profile.apply(ROOT, cap)
            except (ValueError, TypeError):
                return "So geht's: /konto 2500"
            acc = rj(os.path.join(AP, "account.json"), {}) or {}
            acc.update({"start_capital": lim["budget"], "since": datetime.date.today().isoformat()})
            with open(os.path.join(AP, "account.json"), "w") as f:
                json.dump(acc, f)
            return ("Konto auf %s gestellt: Risiko %s pro Trade, Tagesgrenze %s. Gilt ab dem nächsten Handelstag."
                    % (usd(lim["budget"]), usd(lim["max_loss_per_trade_usd"]), usd(lim["max_daily_loss_usd"])))
        return "Unbekannter Befehl. /hilfe zeigt alle."

    def weekly_export(self):
        """Every Friday after the US close (from 16:30 New York), once."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("tb_alerts", os.path.join(ROOT, "trader-alerts", "alerts.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ny = m.to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
        if ny.weekday() != 4 or ny.hour * 60 + ny.minute < 16 * 60 + 30:
            return
        mark = os.path.join(AP, "weekly_export_sent")
        try:
            last = open(mark).read().strip()
        except OSError:
            last = ""
        if last == ny.date().isoformat():
            return
        self.handle("/export")
        self.tg.send("📦 Das ist der Wochen-Export. Leite ihn an Claude weiter, dann wird ausgewertet, was funktioniert.")
        with open(mark, "w") as f:
            f.write(ny.date().isoformat())

    def run(self, once=False):
        try:
            self.tg.call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])})
        except Exception:
            pass
        while True:
            try:
                for chat, text in self.tg.updates(timeout=1 if once else 25):
                    if chat != self.tg.chat or not text.startswith("/"):
                        continue          # only your own chat, only commands
                    try:
                        answer = self.handle(text)
                    except Exception as e:
                        answer = "Fehler bei %s: %s" % (text.split()[0], str(e)[:200])
                    if answer:
                        self.tg.send(answer)
                self.weekly_export()
            except Exception as e:
                sys.stderr.write("telegram bot: %s\n" % str(e)[:200])
                time.sleep(10)
            if once:
                return


def main():
    tok, chat = notify._env()
    if not (tok and chat):
        print("Telegram ist nicht eingerichtet (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen).")
        return 1
    Bot(Telegram(tok, chat)).run()


if __name__ == "__main__":
    sys.exit(main())
