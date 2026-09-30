@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ==== Apex Trade AI - one-click setup ====
echo.

set "PY="
py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY (py -3.12 --version >nul 2>&1 && set "PY=py -3.12")
if not defined PY (
  echo [1/4] Python 3.11 not found - installing it with winget, please wait...
  winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
  py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
)
if not defined PY (
  echo.
  echo Could not install Python automatically.
  echo Install it from https://www.python.org/downloads/release/python-3119/
  echo  ^(tick "Add python.exe to PATH"^), then run setup.bat again.
  pause
  exit /b 1
)
echo [1/4] Using %PY%

if not exist ".venv\Scripts\python.exe" (
  echo [2/4] Creating the Python environment...
  %PY% -m venv .venv || (echo Failed to create .venv & pause & exit /b 1)
) else (
  echo [2/4] Python environment already exists
)

echo [3/4] Installing libraries - this takes a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Library installation FAILED. Send a photo of the red text above.
  pause
  exit /b 1
)

if not exist ".env" copy ".env.example" ".env" >nul
findstr /R /C:"^HELIUS_API_KEY=[A-Za-z0-9]" ".env" >nul
if errorlevel 1 (
  echo [4/4] Paste your Helius key after HELIUS_API_KEY= in the window that opens, then save and close it.
  notepad ".env"
) else (
  echo [4/4] Helius key found in .env
)

echo.
echo ==== Setup finished. Double-click start.bat to run. ====
pause
