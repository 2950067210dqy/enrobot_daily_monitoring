@echo off
title Server Health Daily Monitoring - Live Log
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_ROOT%logs\daily_monitoring_task.log" (
    echo The scheduled task log does not exist yet:
    echo %PROJECT_ROOT%logs\daily_monitoring_task.log
    echo.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -Command "Get-Content -LiteralPath '%PROJECT_ROOT%logs\daily_monitoring_task.log' -Tail 50 -Wait"
