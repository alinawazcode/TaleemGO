@echo off
setlocal
cd /d "%~dp0"
title TaleemGo AI WhatsApp Bridge Runner

echo ========================================================
echo TaleemGo AI WhatsApp Bridge Auto-Restart Runner (Windows)
echo ========================================================

:loop
echo.
echo [%date% %time%] Starting WhatsApp bridge (npm start)...
call npm start
set EXITCODE=%ERRORLEVEL%
echo.
echo [%date% %time%] Bridge exited with code %EXITCODE%.
echo Restarting in 5 seconds (Press Ctrl+C to stop)...
timeout /t 5 /nobreak >nul
goto loop

