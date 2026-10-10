@echo off
REM Stop the Sports Hedge live canary processes started by the companion launcher.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-SportsHedge-LiveCanary.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge live canary stop did not complete cleanly. See logs\ for details.
  pause
  exit /b 1
)
endlocal
exit /b 0
