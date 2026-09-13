@echo off
REM Sports Hedge paper-mode demo launcher (Windows).
REM Double-click: starts backend/frontend hidden, waits for health, opens the operator demo.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SportsHedge-Demo.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge demo failed to start. Logs are under the repository logs\ folder.
  echo This window stays open so the error is visible.
  pause
  exit /b 1
)
endlocal
exit /b 0
