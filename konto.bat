@echo off
rem Stellt alle Limits auf die Kontogroesse ein, mit der du spaeter wirklich handeln willst.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set BETRAG=2000
set /p BETRAG=Kontogroesse in Dollar (Enter = 2000): 
python autopilot\autopilot.py profile %BETRAG%
pause
