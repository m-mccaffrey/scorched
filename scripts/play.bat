@echo off
REM Launch Scorched on Windows. Double-click this file, or run it from a prompt
REM with extra options, e.g.  play.bat --fullscreen
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m scorched %*
) else (
    python -m scorched %*
)
if errorlevel 1 pause
