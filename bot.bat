@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
findstr /b /c:"TELEGRAM_BOT_TOKEN=" .env | findstr /r /c:"=..*" >nul || (
  echo No bot token yet - starting setup...
  call setup-bot.bat
  exit /b
)
start "AlphaDuel bot" /min cmd /c bot-forever.bat
echo AlphaDuel bot started in a minimized window. It restarts by itself if it stops.
timeout /t 5 >nul
