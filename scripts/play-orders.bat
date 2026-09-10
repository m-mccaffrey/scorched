@echo off
REM Launch Standing Orders on Windows. Double-click, or pass options such as
REM   play-orders.bat --fullscreen
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m standing_orders %*
    goto finished
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m standing_orders %*
) else (
    python -m standing_orders %*
)
:finished
if errorlevel 1 (
    echo.
    echo  The game exited with an error. If this is the first run, try:
    echo      scripts\setup.bat
    echo.
    pause
)
