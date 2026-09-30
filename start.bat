@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
start "Apex Engine" cmd /k ".venv\Scripts\python.exe" main.py
timeout /t 5 >nul
start "Apex Dashboard" cmd /k ".venv\Scripts\python.exe" -m streamlit run dashboard.py
timeout /t 8 >nul
start "" http://localhost:8501
echo Engine and dashboard started in two new windows. Close those windows to stop.
timeout /t 5 >nul
