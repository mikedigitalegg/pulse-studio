@echo off
setlocal
cd /d "%~dp0"
title Pulse Studio setup

echo.
echo === Pulse Studio setup ===
echo.

rem Find Python 3.10+ (the py launcher first, then python on PATH)
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Python 3.10 or newer was not found.
  echo Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run Setup.bat again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the virtual environment...
  %PY% -m venv .venv || goto :failed
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env - add your AI key there or on the System page in the app.
)

echo.
echo Installing the PulseBridge Remote Script into Ableton Live...
".venv\Scripts\python.exe" pulse_bridge\install.py
echo.
echo If no "User Library:" line appears above, Live's library wasn't found. Run this with your path:
echo   .venv\Scripts\python.exe pulse_bridge\install.py --user-library "D:\Music\Ableton\User Library"

echo.
echo === Setup finished ===
echo.
echo Next, in Ableton Live: Settings ^> Link, Tempo ^& MIDI ^> Control Surface ^> PulseBridge
echo (restart Live first if it was open). Then double-click Start.bat.
echo.
pause
exit /b 0

:failed
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
