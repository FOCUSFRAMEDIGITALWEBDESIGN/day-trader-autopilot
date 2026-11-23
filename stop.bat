@echo off
taskkill /FI "WINDOWTITLE eq DTA-Autopilot*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq DTA-Dashboard*" /T /F >nul 2>nul
echo Autopilot und Dashboard gestoppt. Offene Paper-Positionen bitte im Alpaca-Dashboard pruefen.
pause
