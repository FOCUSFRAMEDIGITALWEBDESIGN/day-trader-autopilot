@echo off
rem Verbindet den Autopilot mit deinem Telegram-Bot (Token von @BotFather).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo 1. Oeffne in Telegram den Chat mit @BotFather, schicke /newbot und folge den Schritten.
echo 2. BotFather schickt dir einen Token (sieht aus wie 123456789:AA...). Kopiere ihn.
echo.
set /p TOKEN=Token hier einfuegen und Enter druecken: 
python autopilot\notify.py setup %TOKEN%
pause
