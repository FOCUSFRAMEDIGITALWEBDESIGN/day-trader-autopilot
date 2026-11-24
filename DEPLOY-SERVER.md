# Auf deinem Cloud-Server (one.com) starten

Läuft neben deinem anderen Bot. Voraussetzung: Linux (Ubuntu/Debian) mit SSH-Zugang und `sudo`.

> **Vorher:** Bot auf dem PC beenden (`stop.bat`, und `uninstall-autostart.bat`), und dem Server-Bot ein
> **eigenes Alpaca-Paper-Konto** geben, falls dein anderer Bot schon Alpaca benutzt.

## Variante A: ZIP hochladen (ohne GitHub)

1. `day-trader-autopilot.zip` auf den Server laden, z. B. mit **WinSCP** oder **FileZilla** (Protokoll SFTP,
   Server-IP, dein SSH-Benutzer) in dein Home-Verzeichnis, oder in der Windows-Eingabeaufforderung:
   `scp day-trader-autopilot.zip BENUTZER@SERVER-IP:~/`
2. Auf dem Server auspacken und installieren:

```bash
sudo python3 -m zipfile -e ~/day-trader-autopilot.zip /opt/
```

```bash
sudo bash /opt/day-trader-autopilot/deploy/install-server.sh
```

Update später: neue ZIP hochladen, dann `sudo bash /opt/day-trader-autopilot/deploy/update-server.sh ~/day-trader-autopilot.zip`

## Variante B: von GitHub

### 1. Zugang zum privaten GitHub-Repository (einmal)

GitHub → Profilbild → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token**: Repository access *Only select repositories* → `day-trader-autopilot`,
Permissions → *Contents: Read-only*. Den Token kopieren (er ist dein Passwort für `git`).

### 2. Holen und installieren (per SSH auf dem Server)

```bash
sudo apt-get update && sudo apt-get install -y git python3
```

```bash
cd /opt && sudo git clone https://github.com/FOCUSFRAMEDIGITALWEBDESIGN/day-trader-autopilot.git
```

(Benutzername: dein GitHub-Name, Passwort: der Token aus Schritt 1.)

Ohne eigene Domain:

```bash
sudo bash /opt/day-trader-autopilot/deploy/install-server.sh
```

Mit Domain und HTTPS (vorher bei one.com einen A-Record, z. B. `trading.deine-domain.de`, auf die Server-IP setzen):

```bash
sudo bash /opt/day-trader-autopilot/deploy/install-server.sh --domain trading.deine-domain.de
```

Das Skript fragt nach deinen Alpaca-**Paper**-Schlüsseln (Eingabe unsichtbar), erzeugt ein Dashboard-Passwort,
richtet den Dienst ein und zeigt am Ende den **Dashboard-Link** (mit Passwort, als Lesezeichen speichern).

- Port belegt (z. B. durch den anderen Bot)? Mit `--port 8877` einen anderen nehmen.
- Ohne Domain: im one.com-Kundenbereich in der Server-Firewall den Port (8765) eingehend erlauben.
- Laufen auf dem Server schon ein Webserver auf Port 80/443 (nginx/Apache), zeigt das Skript die Zeile, die du
  dort für die Weiterleitung einträgst.

## Nur über einen Cloudflare-Tunnel (empfohlen, wenn du schon cloudflared nutzt)

```bash
sudo bash /opt/day-trader-autopilot/deploy/install-server.sh --tunnel
```

```bash
pm2 start cloudflared --name trader-tunnel -- tunnel --no-autoupdate --url http://localhost:8765 && pm2 save
```

Das Dashboard lauscht dann nur intern (127.0.0.1), kein Port ist offen. Ändert sich die Tunnel-Adresse
(nach einem Neustart), schickt dir der Bot die neue per Telegram, auch die anderer Tunnel auf dem Server.

## Bedienen

| Was | Befehl |
| --- | --- |
| Live-Log | `journalctl -u day-trader-autopilot -f` |
| Status | `systemctl status day-trader-autopilot` |
| Stoppen / Starten | `sudo systemctl stop day-trader-autopilot` / `sudo systemctl start day-trader-autopilot` |
| Update von GitHub | `sudo bash /opt/day-trader-autopilot/deploy/update-server.sh` |
| Kontogröße ändern | `cd /opt/day-trader-autopilot && sudo -u daytrader python3 autopilot/autopilot.py profile 2500` |
| Telegram verbinden | `cd /opt/day-trader-autopilot && sudo -u daytrader python3 autopilot/notify.py setup DEIN_BOT_TOKEN`, dann `sudo systemctl restart day-trader-autopilot` |
| Dashboard-Passwort ansehen | `sudo grep DASHBOARD_TOKEN /opt/day-trader-autopilot/.env` |

Der Wochen-Export für die Auswertung: im Dashboard unten bei **Statistik → Export-Datei herunterladen**.

Der Dienst startet nach jedem Server-Neustart von selbst und nach Abstürzen neu. Deine Daten liegen in
`/opt/day-trader-autopilot/trader/` und bleiben bei Updates erhalten.
