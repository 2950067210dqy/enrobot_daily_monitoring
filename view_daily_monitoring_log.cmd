@echo off
title Server Health Daily Monitoring - Live Log
cd /d "D:\dqy\workspace\monitoring_project"

if not exist "logs\daily_monitoring_task.log" (
    echo The scheduled task log does not exist yet:
    echo D:\dqy\workspace\monitoring_project\logs\daily_monitoring_task.log
    echo.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -Command "Get-Content -LiteralPath 'D:\dqy\workspace\monitoring_project\logs\daily_monitoring_task.log' -Tail 50 -Wait"
