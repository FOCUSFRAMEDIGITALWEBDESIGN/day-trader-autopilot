# Day Trader Autopilot (Alpaca Paper)

Vollautomatische Version des [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)-Pakets
(MIT, Jake Schincariol): die 11 Skills plus ein **Autopilot**, der den beschriebenen Ablauf selbstständig
auf einem **Alpaca Paper-Konto** durchläuft, und ein **Dashboard**, das alles anzeigt.

```
09:00      Scanner: Pre-Market-Scan aller ~5.900 US-Aktien → die meistgehandelten Treffer auf die Tagesliste
ab 09:50   alle 5 Minuten, 25 s nach Kerzenschluss:
           Scanner (ORB) → News → Entry → Exit → Time Machine → Budget → Risk → Broker (Bracket-Order)
laufend    Stop-Loss / Take-Profit liegen als Bracket bei Alpaca; Fills werden protokolliert
15:55      alles Offene wird glattgestellt (keine Übernacht-Positionen)
```

Jeder Schritt ist das **Original-Tool des Pakets** (`trader-*/…py`) mit denselben Dateien in `trader/`.
Ersetzt wird nur das manuelle „yes“ im Broker-Schritt – und nur für Paper. Alle anderen Sicherungen bleiben:
trader-risk muss jede exakte Order mit PASS freigeben, die Time Machine muss einen Vorteil zeigen.

## Sicherheit: nur Paper

- Der Alpaca-Client spricht **ausschließlich** `paper-api.alpaca.markets`; Live-URLs werden abgelehnt.
- Die Kontonummer muss mit `PA` beginnen (Alpaca-Paper-Konten), sonst wird nicht gehandelt.
- `"paper": false` in `trader/config.json` → der Autopilot startet nicht.
- Keys nur in `.env` / Umgebungsvariablen, nie im Chat, nie im Log.
- Jede automatische Freigabe steht in `trader/state/broker.json` mit `"approved_by": "autopilot"`.

## In der Cloud (Railway)

Siehe [DEPLOY-RAILWAY.md](DEPLOY-RAILWAY.md): Repository mit Railway verbinden, vier Variablen setzen,
Volume auf `/app/trader`, Domain erzeugen. Der PC kann dann aus bleiben.

## Einrichten unter Windows

1. Python installieren: <https://www.python.org/downloads/>, Haken bei **„Add python.exe to PATH“**.
2. Doppelklick auf **`start.bat`**: Beim ersten Mal wird `.env` angelegt und in Notepad geöffnet.
   Paper-Key und Secret eintragen, speichern, Notepad schließen.
3. Noch einmal **`start.bat`**: Autopilot und Dashboard starten, der Browser öffnet <http://127.0.0.1:8765>.
4. Beenden mit **`stop.bat`**.

Der PC darf zwischen 15:30 und 22:00 Uhr (US-Handelszeit) nicht in den Energiesparmodus gehen.

## Einrichten (Mac, Linux, Raspberry Pi oder kleiner Server)

Python 3.9+; nichts zu installieren (nur Standardbibliothek).

1. Alpaca-Konto → **Paper Trading** → API Keys erzeugen.
2. ```bash
   cp .env.example .env      # Keys in .env eintragen (Datei bleibt lokal)
   ```
3. ```bash
   ./start.sh
   ```
4. Dashboard öffnen: <http://127.0.0.1:8765>

Der Rechner muss während der US-Handelszeit (15:30–22:00 deutscher Zeit) laufen. Dauerbetrieb als Dienst:
`deploy/*.service` (systemd). Dashboard vom iPad aus: `DASHBOARD_TOKEN` in `.env` setzen, mit
`DASHBOARD_HOST=0.0.0.0 ./start.sh` starten und `http://<rechner-ip>:8765/?token=…` öffnen
(nur im eigenen Netz oder über VPN/Tailscale – nicht offen ins Internet stellen).

Steuerung: `./stop.sh`, `python3 autopilot/autopilot.py pause|resume|status`, oder der Pause-Knopf im Dashboard.
Pausieren stoppt nur **neue** Einstiege; offene Trades werden weiter verwaltet und um 15:55 geschlossen.

## Dashboard

Status (Phase, Paper-Badge, New-York-Zeit, Herzschlag), Equity + Tagesveränderung, realisierter P&L,
Trades/Tageslimit, verbrauchtes Tagesrisiko, offene Positionen mit Stop–Einstieg–Ziel-Leiste,
Pipeline-Trichter (wie viele Kandidaten jede Stufe bestanden haben), **jeder Kandidat mit jedem Schritt
der Kette** (✓/✗ und Grund) inkl. Time-Machine-Quoten, Equity-Kurve, Ereignislog, Trade-Historie
(R und $) und die aktiven Einstellungen. Hell/Dunkel, läuft auch auf dem Handy.

## Einstellungen

`trader/config.json` – Limits des Pakets (Budget, max. Verlust pro Trade, Tagesverlust, Trades/Tag,
Einstiegsfenster, flat by). `trader/autopilot.json` – Strategie und Filter des Autopiloten.
Beide werden im laufenden Betrieb neu eingelesen. Die Startwerte liegen in `config/`.

## Abstimmung (tuning/)

```bash
python3 tuning/fetch.py        # 60 Tage 5-Min-Kerzen von ~50 liquiden US-Aktien (Yahoo)
python3 tuning/backtest.py     # Raster über Range, Stop, Ziel, Einstiegsfenster, Filter; In-/Out-of-Sample
python3 tuning/gate_eval.py    # Time Machine als Filter, walk-forward ohne Blick in die Zukunft
```

Ergebnisse und die gewählte Einstellung: siehe `tuning/TUNING.md`.

## Realistisches Konto ($2.000)

Das Alpaca-Paper-Konto hat $100.000, der Bot handelt aber so, als hätte er nur das Geld, mit dem du später
wirklich handeln willst (Standard **$2.000**, ändern mit `konto.bat`): 1 % Risiko pro Trade ($20), 3 %
Tagesverlust ($60), größte Position 75 % ($1.500). Das **simulierte Konto** (Start + alle Gewinne und Verluste)
steht oben im Dashboard. Fällt es unter **$2.000**, handelt der Bot an dem Tag **nur Long**, weil Alpaca
Leerverkäufe erst ab $2.000 erlaubt.

## Zuverlässig auf dem PC, Telegram, Einstieg, Ereignisse, zweites Setup

- **Wächter:** `start.bat` startet einen Wächter, der Bot und Dashboard nach Abstürzen neu startet, nur eine Kopie
  zulässt und Windows während der US-Handelszeit (9:00–16:15 New York) **wach hält** (der Bildschirm darf aus).
- **Autostart:** `install-autostart.bat` (einmal) startet den Bot bei jeder Windows-Anmeldung, `uninstall-autostart.bat` entfernt das.
- **Telegram:** `telegram-setup.bat` verbindet deinen Telegram-Bot (Token von @BotFather). Dann kommen Nachrichten zu
  jedem Trade, jedem Ausstieg, Fehlern, Neustarts, dem wöchentlichen Lernen und eine Tageszusammenfassung.
- **Limit-Einstieg:** statt Market-Order eine Limit-Order knapp über dem Signal (`entry_limit_buffer_R`, 0,1 R);
  nach `entry_limit_timeout_min` (5) Minuten ohne Ausführung wird sie storniert („verpasst“). Die Statistik zeigt
  Slippage und verpasste Einstiege.
- **Ereignistage:** `config/events.json` (Fed-Zinsentscheide, US-Inflationsdaten; Quellen in der Datei). Am Fed-Tag
  keine Einstiege nach 12:00 und flat um 13:55 (`event_actions`). Aktien mit Quartalszahlen am selben Tag werden
  ausgelassen (Nasdaq-Kalender, `earnings_action`).
- **Setup B:** 5-Minuten-Opening-Range nur auf den Stocks in Play, Einstiege 9:40–10:30 (`setup_b`). Das Lernmodul
  bewertet es jede Woche getrennt und schaltet es bei klarer Datenlage ein oder aus.

## Long und Short, Stocks in Play

- **Short-Seite:** Der Bot handelt auch Ausbrüche nach **unten** (Schlusskurs unter dem Opening-Range-Tief),
  gespiegelt mit denselben Regeln: Stop über dem Einstieg, Time Machine (Seite „sell“), trader-risk mit
  `allow_short` nur für diese Orders (deine `config.json` bleibt unverändert), Rückkauf spätestens 15:55.
  Einstellung `sides`: `"long"`, `"short"` oder `"both"` (Standard). Die Richtung ändert das Lernmodul nur
  auf Basis von Jahresdaten.
- **Stocks in Play:** Um 9:46 New York ordnet der Bot ~1.500 liquide US-Aktien (wöchentlich aus Alpaca-Tagesdaten
  bestimmt) nach Kurslücke und relativem Volumen und nimmt die stärksten 30 in die Scanliste
  (`in_play_*` in `trader/autopilot.json`).

## Statistik und Wochen-Export

Der Abschnitt **Statistik** im Dashboard zeigt alle geschlossenen Paper-Trades nach Richtung, Uhrzeit,
Ausstiegsart, Stocks in Play, relativem Volumen, Markt, Stärke, Time-Machine-Edge, Wochentag, Plan-Version
und Aktie, dazu wo die Kandidaten hängen blieben und Live gegen Backtest.

**Export für die Auswertung:** Doppelklick auf `export.bat` (oder der Knopf im Dashboard). Es entsteht
`trader/export/autopilot-export-DATUM.zip` – ohne Alpaca-Schlüssel und ohne persönliche Daten. Ausgewertet
wird sie mit `python tuning/analyze_export.py DATEI.zip`: echte Trades plus Gegenprobe, was die
abgelehnten Kandidaten auf den echten Kerzen gemacht hätten.

## Trade-Fenster (Charts wie bei TradingView)

Für jeden offenen Trade erscheint im Dashboard ein eigenes Chart-Fenster, live aktualisiert: Kerzen
(1 oder 5 Minuten), Volumen, Opening Range, die grüne Ziel- und rote Risikozone wie beim
„Long Position“-Werkzeug von TradingView, der Stop als Treppenlinie (jedes Nachziehen sichtbar),
Kauf-, Teilverkauf- und Ausstiegs-Pfeile sowie P&L in $ und R. Jeder frühere Trade lässt sich über
📈 in der Trade-Tabelle als großes Fenster öffnen. Gezeichnet mit TradingView Lightweight Charts™
(Apache 2.0, liegt in `dashboard/vendor/`, funktioniert ohne Internet); die Kerzen kommen von Yahoo.

## Selbst lernen (wöchentlich)

Einmal pro Woche außerhalb der Handelszeit startet der Autopilot `autopilot/learn.py` von selbst
(von Hand: `python autopilot/learn.py`). Es lädt die neuesten 60 Tage, prüft rund 230 Strategie-Varianten
und testet die besten walk-forward mit der Time Machine, so wie der Autopilot handelt.
**Gewechselt wird nur, wenn der neue Plan deutlich besser ist** (vorsichtige Schätzung: Ø R minus
Standardfehler, mindestens +0,03 R Vorsprung, mindestens 25 Trades, Ø R positiv). Sonst bleibt der Plan.
Läuft der aktuelle Plan im Paper-Handel schlecht (≥ 15 Trades, Ø schlechter als −0,30 R), pausiert er neue
Einstiege und schreibt den Grund ins Dashboard.
Das Lernmodul ändert nie Budget, Verlustlimits, Trades pro Tag oder Paper-Modus, nur die Auswahl der Setups.
Abschalten: `"learn_weekly": false` in `trader/autopilot.json`.

## Filter und Ausstiege (vom Lernmodul gewählt)

| Einstellung (`trader/autopilot.json`) | Wirkung |
| --- | --- |
| `rvol_min` | nur Aktien mit hohem **relativem Volumen** in den ersten Minuten (z. B. 1.5 = 150 % des Üblichen) |
| `market_filter: "green"` | nur kaufen, wenn der **Gesamtmarkt (SPY)** seit Eröffnung im Plus ist |
| `rs_min` | Aktie muss seit Eröffnung **stärker als SPY** sein (0 = mindestens gleich stark) |
| `exit_mode: "breakeven"` | ab +1R wird der **Stop auf den Einstieg** gezogen |
| `exit_mode: "partial"` | ab +1R wird die **Hälfte verkauft**, der Rest läuft mit Stop auf Einstieg zum Ziel |
| `exit_mode: "trail_r"` | **Trailing-Stop in R-Stufen**, kein festes Ziel: +1R → Stop auf Einstieg, +2R → Stop auf +1R, … |
| `exit_mode: "trail_atr"` | **Trailing-Stop im ATR-Abstand**, kein festes Ziel: Stop = Hoch seit Einstieg − `exit_atr_k` × ATR |

Beim Trailing wird der Stop nach jeder 5-Minuten-Kerze bei Alpaca nachgezogen, nie nach unten. Das
Take-Profit der Bracket-Order wird auf `trail_cap_R` (10R) als Notbremse hochgesetzt; spätestens 15:55 wird verkauft.

Preise für Größe und Ausstiege kommen in Echtzeit von Alpaca (Yahoo nur als Ersatz).
Das Lernmodul lädt mit deinen Paper-Keys **2 Jahre 5-Minuten-Kerzen von Alpaca** (`learn_years`) und testet
alle Kombinationen darauf; ohne Alpaca-Zugang nimmt es Yahoos 60 Tage. Von Hand:
`python tuning/alpaca_data.py --years 2` und `python tuning/research.py`.

## Tests

```bash
python3 -m unittest discover -s tests
```

320 Offline-Tests: die Original-Suite des Pakets plus ein kompletter Autopilot-Tag (TSLA, 19.10.2026,
echte Daten) gegen einen lokalen Mock-Broker: Einstieg, Take-Profit, Glattstellen um 15:55, Pause,
Time-Machine-Sperre, Live-Sperre.

**Keine Anlageberatung.** Die meisten Daytrader verlieren Geld; ein Backtest ist kein Versprechen.
Yahoo-Daten sind inoffiziell und verzögert. Erst lange auf Paper beobachten.
