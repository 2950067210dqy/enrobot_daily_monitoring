@echo off
setlocal
cd /d "%~dp0"

echo Initializing SSH host keys for the three monitoring servers...
echo Verify every displayed fingerprint before entering yes.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0initialize_ssh_hosts.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo SSH host-key initialization completed successfully.
) else (
    echo SSH host-key initialization failed. Exit code: %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%
