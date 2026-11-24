@echo off
rem Writes the weekly export (no keys, no personal data) and opens its folder.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python autopilot\autopilot.py export
start "" "%~dp0trader\export"
pause
