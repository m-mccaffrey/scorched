@echo off
REM Run a headless Standing Orders server on Windows.
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m standing_orders server %*
    goto finished
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m standing_orders server %*
) else (
    python -m standing_orders server %*
)
:finished
if errorlevel 1 pause
