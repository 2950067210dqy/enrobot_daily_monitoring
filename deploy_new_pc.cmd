@echo off
setlocal
title Deploy Server Health Monitoring

echo This installer will request administrator permission.
echo Project directory: %~dp0
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_new_pc.ps1"
set "DEPLOY_EXIT_CODE=%ERRORLEVEL%"

echo.
if %DEPLOY_EXIT_CODE% EQU 0 (
    echo Deployment completed successfully.
) else (
    echo Deployment failed. Exit code: %DEPLOY_EXIT_CODE%
)
echo.
pause
exit /b %DEPLOY_EXIT_CODE%
