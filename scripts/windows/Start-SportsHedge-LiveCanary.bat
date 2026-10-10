@echo off
REM Sports Hedge Wave 40 live canary (Windows). Interactive confirmation required.
REM Separate from Start-SportsHedge-Real.bat, which keeps execution disabled.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SportsHedge-LiveCanary.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge live canary failed to start. Logs are under the repository logs\ folder.
  echo This window stays open so the error is visible.
  pause
  exit /b 1
)
endlocal
exit /b 0
