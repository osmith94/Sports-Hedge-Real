@echo off
REM Sports Hedge Real dry-run launcher (Windows).
REM Double-click: starts backend/frontend hidden, waits for the Real health gate, opens the console.
REM Live order execution stays disabled.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SportsHedge-Real.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge Real failed to start. Logs are under the repository logs\ folder.
  echo This window stays open so the error is visible.
  pause
  exit /b 1
)
endlocal
exit /b 0
