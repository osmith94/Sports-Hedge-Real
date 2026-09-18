# Stop processes started by Start-SportsHedge-Demo.ps1.
# Never force-kill a reused PID. Identity JSON (pid + path + command tokens)
# must match the live process command/path before Stop-Process.
# Decision contract matches sports_hedge.application.demo_launcher_pid.

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-backend.pid") -Label "backend"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-frontend.pid") -Label "frontend"

Write-Host "Sports Hedge paper demo stop completed. Logs retained under logs\."
exit 0
