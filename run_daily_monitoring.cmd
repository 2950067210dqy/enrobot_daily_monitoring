@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_ROOT%logs" mkdir "%PROJECT_ROOT%logs"

whoami /user | findstr /c:"S-1-5-18" >nul
if %ERRORLEVEL% EQU 0 goto scheduled

title Server Health Daily Monitoring
echo [%date% %time%] Starting run_daily_monitoring.py
echo.
"%PROJECT_ROOT%.venv\Scripts\python.exe" "%PROJECT_ROOT%run_daily_monitoring.py" %*
if %ERRORLEVEL% NEQ 0 goto manual_failed
echo.
echo [%date% %time%] Finished. Exit code: 0
pause
exit /b 0

:manual_failed
echo.
echo [%date% %time%] Finished. Exit code: 1
pause
exit /b 1

:scheduled
set "SERVER_HEALTH_SYSTEM_TASK=1"
"%PROJECT_ROOT%.venv\Scripts\python.exe" "%PROJECT_ROOT%run_daily_monitoring.py" %* >> "%PROJECT_ROOT%logs\daily_monitoring_task.log" 2>&1
if %ERRORLEVEL% NEQ 0 goto scheduled_failed
echo [%date% %time%] run_daily_monitoring.py exit code: 0 >> "%PROJECT_ROOT%logs\daily_monitoring_task.log"
exit /b 0

:scheduled_failed
echo [%date% %time%] run_daily_monitoring.py exit code: 1 >> "%PROJECT_ROOT%logs\daily_monitoring_task.log"
exit /b 1
