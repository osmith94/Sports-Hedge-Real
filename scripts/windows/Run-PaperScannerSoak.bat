@echo off
REM Sports Hedge Phase 6 read-only PAPER scanner soak (Windows).
REM Observes a running paper backend. Never places/cancels/signs venue orders.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-PaperScannerSoak.ps1" %*
if errorlevel 1 (
  echo.
  echo Scanner soak finished with a non-zero exit. Report JSON is under logs\.
  echo This window stays open so the result is visible.
  pause
  exit /b 1
)
endlocal
exit /b 0
