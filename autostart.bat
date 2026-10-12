@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==== AlphaDuel: keep the bot running on this laptop ====
echo.
echo [1/3] Start the bot automatically when Windows starts...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\AlphaDuel bot.lnk'); $s.TargetPath='%~dp0bot.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo [2/3] Never sleep while plugged in (screen may still turn off)...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo [3/3] Closing the lid does nothing while plugged in...
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1
echo.
echo Done. Keep the charger connected and set your VPN app to start with Windows.
echo To undo: run autostart-off.bat
pause
