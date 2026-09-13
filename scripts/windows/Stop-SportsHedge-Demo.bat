@echo off
REM Stop the Sports Hedge paper-mode demo processes started by the companion launcher.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-SportsHedge-Demo.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge demo stop did not complete cleanly. See logs\ for details.
  pause
  exit /b 1
)
endlocal
exit /b 0
