@echo off
rem Day Trader Autopilot fuer Windows: startet Autopilot (nur Paper) und Dashboard
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
  echo Python fehlt. Bitte von https://www.python.org/downloads/ installieren
  echo und beim Installieren den Haken "Add python.exe to PATH" setzen.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo Die Datei .env wurde angelegt und wird jetzt in Notepad geoeffnet.
  echo Trage ALPACA_API_KEY und ALPACA_SECRET_KEY ein, speichere, schliesse Notepad
  echo und starte start.bat noch einmal.
  notepad .env
  pause
  exit /b 0
)

if not exist trader mkdir trader
if not exist trader\autopilot mkdir trader\autopilot
if not exist trader\config.json copy config\config.json trader\config.json >nul
if not exist trader\autopilot.json copy config\autopilot.json trader\autopilot.json >nul

start "DTA-Autopilot" /min cmd /c "python autopilot\autopilot.py run >> trader\autopilot\autopilot.log 2>&1"
start "DTA-Dashboard" /min cmd /c "python dashboard\server.py >> trader\autopilot\dashboard.log 2>&1"
timeout /t 4 >nul
start "" http://127.0.0.1:8765
echo Autopilot und Dashboard laufen (zwei minimierte Fenster). Beenden mit stop.bat.
echo Logs: trader\autopilot\autopilot.log
pause
