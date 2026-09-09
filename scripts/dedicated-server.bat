@echo off
REM Run a headless Scorched server on Windows. Needs no display and no pygame.
REM
REM   dedicated-server.bat --bots 2 --rounds 5
REM
REM Players join with "Find LAN Games", or by typing this machine's address.
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m scorched server %*
    goto finished
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m scorched server %*
) else (
    python -m scorched server %*
)

:finished
if errorlevel 1 pause
