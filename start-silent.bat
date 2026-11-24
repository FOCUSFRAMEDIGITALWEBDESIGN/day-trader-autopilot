@echo off
rem Startet den Waechter ohne Rueckfragen (fuer den Autostart). Laeuft er schon, passiert nichts.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist trader\autopilot mkdir trader\autopilot
start "DTA-Autopilot" /min cmd /c "python deploy\run_all.py --local >> trader\autopilot\run_all.log 2>&1"
