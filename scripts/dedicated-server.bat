@echo off
REM Run a headless Scorched server on Windows.
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m scorched server %*
) else (
    python -m scorched server %*
)
if errorlevel 1 pause
