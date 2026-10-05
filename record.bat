@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import playwright, imageio_ffmpeg" 2>nul
if errorlevel 1 (
  echo First use: installing the recorder (one time, about 200 MB^)...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements-record.txt || (echo Install failed & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m playwright install chromium || (echo Browser install failed & pause & exit /b 1)
)
set MODE=%1
if "%MODE%"=="" set MODE=live
echo Recording ONLY the dashboard page (start.bat must be running). Ctrl+C stops and saves.
".venv\Scripts\python.exe" recorder.py %MODE% %2
echo.
echo Videos are in the "recordings" folder.
start "" "%~dp0recordings"
pause
