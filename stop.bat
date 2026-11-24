@echo off
cd /d "%~dp0"
taskkill /FI "WINDOWTITLE eq DTA-Autopilot*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq DTA-Dashboard*" /T /F >nul 2>nul
if exist trader\autopilot\run_all.lock (
  for /f "usebackq delims=" %%p in ("trader\autopilot\run_all.lock") do taskkill /PID %%p /T /F >nul 2>nul
)
echo Autopilot und Dashboard gestoppt. Offene Paper-Positionen bitte im Alpaca-Dashboard pruefen.
pause
