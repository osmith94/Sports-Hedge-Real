@echo off
REM Stop the Sports Hedge Real processes started by the companion launcher.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-SportsHedge-Real.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge Real stop did not complete cleanly. See logs\ for details.
  pause
  exit /b 1
)
endlocal
exit /b 0
