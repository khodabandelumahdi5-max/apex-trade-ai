@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AlphaDuel bot - do not close
:loop
echo [%date% %time%] starting AlphaDuel bot...
".venv\Scripts\python.exe" -m tgbot
echo [%date% %time%] bot stopped (internet/VPN down?). Restarting in 15 seconds - close this window to stop.
timeout /t 15 >nul
goto loop
