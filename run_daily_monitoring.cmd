@echo off
setlocal
cd /d "D:\dqy\workspace\monitoring_project"

if not exist "logs" mkdir "logs"

whoami /user | findstr /c:"S-1-5-18" >nul
if %ERRORLEVEL% EQU 0 goto scheduled

title Server Health Daily Monitoring
echo [%date% %time%] Starting run_daily_monitoring.py
echo.
"D:\dqy\workspace\monitoring_project\.venv\Scripts\python.exe" "D:\dqy\workspace\monitoring_project\run_daily_monitoring.py" %*
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
"D:\dqy\workspace\monitoring_project\.venv\Scripts\python.exe" "D:\dqy\workspace\monitoring_project\run_daily_monitoring.py" %* >> "D:\dqy\workspace\monitoring_project\logs\daily_monitoring_task.log" 2>&1
if %ERRORLEVEL% NEQ 0 goto scheduled_failed
echo [%date% %time%] run_daily_monitoring.py exit code: 0 >> "D:\dqy\workspace\monitoring_project\logs\daily_monitoring_task.log"
exit /b 0

:scheduled_failed
echo [%date% %time%] run_daily_monitoring.py exit code: 1 >> "D:\dqy\workspace\monitoring_project\logs\daily_monitoring_task.log"
exit /b 1
