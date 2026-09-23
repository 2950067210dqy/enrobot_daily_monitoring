@echo off
setlocal
cd /d "%~dp0"
set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Virtual environment Python was not found:
    echo %PYTHON_EXE%
    echo Run deploy_new_pc.cmd first.
    pause
    exit /b 1
)

echo Python runtime:
"%PYTHON_EXE%" -c "import platform,sys; print(sys.version); print('Architecture: ' + platform.architecture()[0]); print('Executable: ' + sys.executable)"
if errorlevel 1 goto failed

echo.
echo Reinstalling ReportLab and its PNG rendering backend...
"%PYTHON_EXE%" -m pip install --no-cache-dir --force-reinstall -r "%PROJECT_ROOT%requirements.txt"
if errorlevel 1 goto failed

echo.
echo Verifying ReportLab PNG rendering...
"%PYTHON_EXE%" -c "import reportlab; from reportlab.graphics import renderPM; from reportlab.graphics.shapes import Drawing,Rect; d=Drawing(20,20); d.add(Rect(1,1,18,18)); data=renderPM.drawToString(d,fmt='PNG',backend='_renderPM'); print('ReportLab ' + reportlab.Version + ': PNG backend OK (' + str(len(data)) + ' bytes)')"
if errorlevel 1 goto failed

echo.
echo Repair completed. Run run_daily_monitoring.cmd again.
pause
exit /b 0

:failed
set "REPAIR_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Repair failed. Exit code: %REPAIR_EXIT_CODE%
pause
exit /b %REPAIR_EXIT_CODE%
