@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
findstr /b /c:"TELEGRAM_BOT_TOKEN=" .env | findstr /r /c:"=..*" >nul || (
  echo Put your bot token in .env:  TELEGRAM_BOT_TOKEN=...   ^(from @BotFather, never share it^)
  pause
  exit /b 1
)
start "AlphaDuel bot" cmd /k ".venv\Scripts\python.exe" -m tgbot
echo AlphaDuel bot started in a new window. Open https://t.me/alphaduellivebot and send /start
timeout /t 8 >nul
