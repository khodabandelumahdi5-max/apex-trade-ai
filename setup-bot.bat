@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Installing Python environment, please wait...
  call setup.bat
)
echo Updating, please wait...
git pull -q 2>nul
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" -m tgbot.setup_wizard
if errorlevel 1 (
  echo Setup was cancelled.
  pause
  exit /b 1
)
call bot.bat
