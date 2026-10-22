@echo off
setlocal
cd /d "%~dp0"
title Pulse Studio server

if not exist ".venv\Scripts\python.exe" (
  echo Pulse Studio isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b 1
)

echo Starting Pulse Studio on http://127.0.0.1:8005/
echo Leave this window open while you use the app. Close it (or press Ctrl+C) to stop.
echo.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8005/"
".venv\Scripts\python.exe" -m uvicorn ableton_web_poc_server:app --host 127.0.0.1 --port 8005
if errorlevel 1 pause
