@echo off
REM ---------------------------------------------------------------------------
REM  Set Scorched up on Windows.
REM
REM  Double-click this file, or run it from a prompt with options:
REM      setup.bat --venv        install into a .venv instead of system Python
REM      setup.bat --dev         also install the test tools
REM      setup.bat --firewall    open the LAN ports, needs Administrator
REM
REM  Safe to re-run: it verifies rather than reinstalls.
REM ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0.."

REM pygame prints a greeting banner on import, which would otherwise land in
REM the middle of the values this script reads back.
set "PYGAME_HIDE_SUPPORT_PROMPT=1"

set "MODE=auto"
set "WITH_DEV="
set "DO_FIREWALL="

:parse
if "%~1"=="" goto parsed
set "ARG_OK="
if /i "%~1"=="--venv"     ( set "MODE=venv" & set "ARG_OK=1" )
if /i "%~1"=="--system"   ( set "MODE=system" & set "ARG_OK=1" )
if /i "%~1"=="--dev"      ( set "WITH_DEV=1" & set "ARG_OK=1" )
if /i "%~1"=="--firewall" ( set "DO_FIREWALL=1" & set "ARG_OK=1" )
if /i "%~1"=="--help"     goto usage
if /i "%~1"=="-h"         goto usage
if /i "%~1"=="/?"         goto usage
if not defined ARG_OK goto bad_arg
shift
goto parse
:parsed

echo.
echo  Scorched setup
echo  ==============
echo.

REM --- 1. find a usable Python ----------------------------------------------
echo ==^> Looking for Python 3.9 or newer
set "PY="
call :try_python py -3
call :try_python python
call :try_python python3
if not defined PY goto no_python

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo     Python !PYVER!

REM --- 2. is pygame already here? -------------------------------------------
echo ==^> Checking for pygame
if /i "%MODE%"=="venv" goto make_venv
if /i "%MODE%"=="system" goto do_install
call :have_pygame
if "!HAVE_PYGAME!"=="1" (
    echo     already present
    goto verify
)
echo     not installed

REM --- 3. install ------------------------------------------------------------
:do_install
echo ==^> Installing pygame
echo     this downloads about 12 MB
%PY% -m pip install --upgrade pip >nul 2>nul
%PY% -m pip install --retries 5 --timeout 30 -r requirements.txt
if errorlevel 1 (
    echo.
    echo     System install failed. Falling back to a virtual environment.
    echo.
    goto make_venv
)
goto after_install

:make_venv
echo ==^> Creating a virtual environment in .venv
if exist ".venv\Scripts\python.exe" (
    echo     reusing the existing environment
) else (
    %PY% -m venv .venv
    if errorlevel 1 goto venv_failed
)
set "PY=.venv\Scripts\python.exe"
echo ==^> Installing pygame
echo     this downloads about 12 MB
%PY% -m pip install --upgrade pip >nul 2>nul
%PY% -m pip install --retries 5 --timeout 30 -r requirements.txt
if errorlevel 1 goto download_failed

:after_install
if defined WITH_DEV (
    echo ==^> Installing the test tools
    %PY% -m pip install --retries 5 --timeout 30 pytest pyflakes
    if errorlevel 1 echo     could not install the test tools; the game itself is unaffected
)

REM --- 4. verify -------------------------------------------------------------
:verify
echo ==^> Verifying the installation
REM The dummy drivers let this succeed over Remote Desktop and on a machine
REM with no sound card, so a real problem is never masked.
set "SDL_VIDEODRIVER=dummy"
set "SDL_AUDIODRIVER=dummy"
%PY% -c "import pygame;pygame.init();pygame.display.set_mode((320,200));print('    pygame',pygame.version.ver,'works')"
set "VERIFY_RC=!errorlevel!"
set "SDL_VIDEODRIVER="
set "SDL_AUDIODRIVER="
if not "!VERIFY_RC!"=="0" goto pygame_broken

%PY% -m scorched --version >nul 2>nul
if errorlevel 1 goto game_broken
%PY% -m standing_orders --version >nul 2>nul
if errorlevel 1 goto game_broken
echo     both games start

REM --- 5. optional firewall rules -------------------------------------------
if defined DO_FIREWALL call :firewall

REM --- 6. what next ----------------------------------------------------------
set "LANIP="
for /f "delims=" %%i in ('%PY% -c "from lanlib.discovery import local_addresses;print(local_addresses()[0])" 2^>nul') do set "LANIP=%%i"
if not defined LANIP set "LANIP=<the LAN address of this machine>"

echo.
echo  Setup complete.
echo.
echo    Scorched:         scripts\play.bat
echo    Standing Orders:  scripts\play-orders.bat
echo.
echo    Headless servers: scripts\dedicated-server.bat --bots 2
echo                      scripts\orders-server.bat --bots 2
echo.
echo    Hosting? Other players pick "Find LAN Games", or type your address:
echo        !LANIP!
echo.
if not defined DO_FIREWALL (
    echo    The first time you host, Windows will ask to allow the game
    echo    through the firewall. Tick "Private networks" and accept.
    echo    To do it now instead, re-run as Administrator with --firewall
    echo.
)
goto done

REM ===========================================================================
REM  subroutines
REM ===========================================================================

:try_python
REM Probe a candidate interpreter by asking it to prove its own version. This
REM also weeds out the Microsoft Store stub, which is on PATH as "python" even
REM when Python is not actually installed.
if defined PY goto :eof
%* -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%*"
goto :eof

:have_pygame
set "HAVE_PYGAME="
%PY% -c "import pygame,sys; sys.exit(0 if tuple(int(p) for p in pygame.version.ver.split('.')[:2]) >= (2,0) else 1)" >nul 2>nul
if not errorlevel 1 set "HAVE_PYGAME=1"
goto :eof

:firewall
echo ==^> Adding firewall rules for LAN play
net session >nul 2>nul
if errorlevel 1 (
    echo     This needs an Administrator prompt. Right-click setup.bat and
    echo     choose "Run as administrator", or run these two commands in an
    echo     elevated prompt:
    echo.
    echo       netsh advfirewall firewall add rule name="Scorched TCP" dir=in action=allow protocol=TCP localport=27015
    echo       netsh advfirewall firewall add rule name="Scorched UDP" dir=in action=allow protocol=UDP localport=27016
    goto :eof
)
netsh advfirewall firewall add rule name="Scorched TCP" dir=in action=allow protocol=TCP localport=27015 >nul
netsh advfirewall firewall add rule name="Scorched UDP" dir=in action=allow protocol=UDP localport=27016 >nul
echo     TCP 27015 and UDP 27016 are open
goto :eof

REM ===========================================================================
REM  failure paths
REM ===========================================================================

:no_python
echo.
echo  error: no suitable Python was found.
echo.
echo  Install Python 3.9 or newer from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" in the installer, then run this
echo  script again.
echo.
echo  Note: if typing "python" opens the Microsoft Store, that is a
echo  placeholder, not a real install.
goto fail

:venv_failed
echo.
echo  error: could not create a virtual environment.
echo  Try installing into the system Python instead:
echo      setup.bat --system
goto fail

:download_failed
echo.
echo  error: could not download pygame.
echo  This is usually a dropped or filtered connection. Check the network
echo  and run setup.bat again.
goto fail

:pygame_broken
echo.
echo  error: pygame installed but will not start.
echo  Try reinstalling it:
echo      %PY% -m pip install --force-reinstall pygame-ce
goto fail

:game_broken
echo.
echo  error: the game failed its own start-up check.
echo  Run this to see why:
echo      %PY% -m scorched --version
goto fail

:bad_arg
echo.
echo  error: unknown option "%~1"
echo  Run "setup.bat --help" to see the available options.
goto fail

:usage
echo Usage: setup.bat [options]
echo.
echo Installs everything Scorched needs. Safe to re-run.
echo.
echo   --venv        Always use a virtual environment in .venv
echo   --system      Always install into the system Python
echo   --dev         Also install the test tools, pytest and pyflakes
echo   --firewall    Open TCP 27015 and UDP 27016, needs Administrator
echo   --help        Show this message
echo.
echo After setup, start the game with:  scripts\play.bat
goto done

:fail
echo.
call :maybe_pause
exit /b 1

:done
call :maybe_pause
exit /b 0

:maybe_pause
REM Pause only when launched from Explorer, so running this from a script or
REM a prompt does not block waiting for a keypress.
echo(%cmdcmdline% | find /i "/c" >nul
if not errorlevel 1 pause
goto :eof
