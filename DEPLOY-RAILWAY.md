# Auf Railway starten (über GitHub)

Der Bot läuft dann rund um die Uhr in der Cloud, dein PC kann aus bleiben.

> **Wichtig:** Stoppe vorher den Bot auf deinem PC (`stop.bat`). Zwei Bots auf demselben
> Alpaca-Paper-Konto würden doppelt handeln.

## 1. Projekt anlegen

1. <https://railway.com> → mit GitHub anmelden.
2. **New Project → Deploy from GitHub repo** → dieses Repository wählen.
   Railway erkennt das `Dockerfile` und `railway.json` von selbst.

## 2. Variablen setzen (Service → **Variables**)

| Name | Wert |
| --- | --- |
| `ALPACA_API_KEY` | dein Alpaca **Paper**-Key (`PK…`) |
| `ALPACA_SECRET_KEY` | dein Alpaca **Paper**-Secret |
| `ALPACA_PAPER_TRADE` | `true` |
| `DASHBOARD_TOKEN` | ein langes Passwort, mind. 16 Zeichen (z. B. 30 zufällige Buchstaben/Ziffern) |

Die Schlüssel stehen nur in Railway, nie im Repository.

## 3. Volume für die Daten (Service → **Settings → Volumes** oder Rechtsklick auf den Service → **Attach Volume**)

- Mount path: **`/app/trader`**

Dort liegen Trades, Statistik, Einstellungen und Logs. Ohne Volume wären sie nach jedem Neustart weg.

## 4. Öffentliche Adresse (Service → **Settings → Networking → Generate Domain**)

Dashboard öffnen: `https://DEIN-NAME.up.railway.app/?token=DEIN_DASHBOARD_TOKEN`
(danach merkt sich der Browser den Zugang; auch auf dem iPad als Lesezeichen speicherbar).

## 5. Prüfen

- **Deployments → View Logs**: `[run_all] started dashboard …` und `autopilot started (paper only …)`.
- Im Dashboard oben: „aktualisiert vor X s“, Konto `PA…`.

## Updates

Jeder Push auf GitHub baut und startet den Bot neu; die Daten im Volume bleiben erhalten.
Einstellungen in `trader/autopilot.json` stammen beim ersten Start aus `config/`; spätere Änderungen
an `config/` wirken nicht mehr auf ein bestehendes Volume (das Lernmodul pflegt `trader/autopilot.json`).

## Wochen-Export

Im Dashboard unten bei **Statistik → Export-Datei herunterladen**.

## Kosten

Ein kleiner Python-Prozess ohne Datenbank; im Railway-Hobby-Tarif üblicherweise im Bereich weniger Dollar pro Monat.
Aktuelle Preise: <https://railway.com/pricing>.
