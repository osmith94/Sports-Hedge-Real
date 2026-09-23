@echo off
REM Refresh the Sports Hedge paper-mode demo onto latest owner-live.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Refresh-SportsHedge-Demo.ps1"
if errorlevel 1 (
  echo.
  echo Sports Hedge demo refresh did not complete cleanly. frontend\.next was not deleted unless stop and git succeeded.
  echo This window stays open so the error is visible.
  pause
  exit /b 1
)
endlocal
exit /b 0
