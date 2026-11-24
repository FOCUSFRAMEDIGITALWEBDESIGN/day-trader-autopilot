@echo off
rem Startet den Autopilot automatisch, sobald du dich bei Windows anmeldest.
cd /d "%~dp0"
schtasks /Create /TN "DayTraderAutopilot" /TR "\"%~dp0start-silent.bat\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
  echo Das hat nicht geklappt. Rechtsklick auf install-autostart.bat und "Als Administrator ausfuehren".
) else (
  echo Autostart eingerichtet: Der Autopilot startet bei jeder Anmeldung von selbst.
  echo Entfernen mit uninstall-autostart.bat
)
pause
