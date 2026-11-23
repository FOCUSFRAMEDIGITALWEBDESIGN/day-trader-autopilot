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

## Tests

```bash
python3 -m unittest discover -s tests
```

304 Offline-Tests: die Original-Suite des Pakets plus ein kompletter Autopilot-Tag (TSLA, 19.10.2026,
echte Daten) gegen einen lokalen Mock-Broker: Einstieg, Take-Profit, Glattstellen um 15:55, Pause,
Time-Machine-Sperre, Live-Sperre.

**Keine Anlageberatung.** Die meisten Daytrader verlieren Geld; ein Backtest ist kein Versprechen.
Yahoo-Daten sind inoffiziell und verzögert. Erst lange auf Paper beobachten.
