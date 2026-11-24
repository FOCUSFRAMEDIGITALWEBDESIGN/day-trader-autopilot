@echo off
rem Day Trader Autopilot fuer Windows: startet Autopilot (nur Paper) und Dashboard unter einem Waechter
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

call "%~dp0start-silent.bat"
timeout /t 5 >nul
start "" http://127.0.0.1:8765
echo.
echo Der Autopilot laeuft im Hintergrund (minimiertes Fenster "DTA-Autopilot").
echo Er startet sich nach Abstuerzen selbst neu und haelt Windows waehrend der US-Handelszeit wach.
echo Dashboard: http://127.0.0.1:8765     Beenden: stop.bat
echo Logs: trader\autopilot\autopilot.log und trader\autopilot\run_all.log
pause
