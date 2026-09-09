@echo off
REM Launch Scorched on Windows. Double-click this file, or run it from a
REM prompt with extra options, e.g.  play.bat --fullscreen
REM
REM If you have not set up yet, run scripts\setup.bat first.
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m scorched %*
    goto finished
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m scorched %*
) else (
    python -m scorched %*
)

:finished
if errorlevel 1 (
    echo.
    echo  The game exited with an error. If this is the first run, try:
    echo      scripts\setup.bat
    echo.
    pause
)
