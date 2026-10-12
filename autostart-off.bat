@echo off
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AlphaDuel bot.lnk" 2>nul
powercfg /change standby-timeout-ac 30
echo Autostart removed, sleep after 30 minutes on charger restored.
pause
