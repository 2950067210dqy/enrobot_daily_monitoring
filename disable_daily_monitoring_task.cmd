@echo off
setlocal
title Disable Server Health Daily Monitoring Task

net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Requesting administrator permission...
    powershell.exe -NoLogo -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Disabling scheduled task: ServerHealthDailyMonitoring
schtasks.exe /Change /TN "ServerHealthDailyMonitoring" /DISABLE
set "TASK_EXIT_CODE=%ERRORLEVEL%"

if %TASK_EXIT_CODE% EQU 0 (
    echo.
    echo Scheduled task disabled successfully.
) else (
    echo.
    echo Failed to disable scheduled task. Exit code: %TASK_EXIT_CODE%
)

echo.
pause
exit /b %TASK_EXIT_CODE%
