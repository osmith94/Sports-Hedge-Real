# Stop processes started by Start-SportsHedge-LiveCanary.ps1.
# Uses live-canary PID files only. Real dry-run and Demo PID files are left untouched.
# Halt of new orders is a backend POST; this stop does not cancel venue orders
# and must not be described as a venue cancel.

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
Stop-DemoPid -PidFile (Join-Path $Logs "live-canary-backend.pid") -Label "backend"
Stop-DemoPid -PidFile (Join-Path $Logs "live-canary-frontend.pid") -Label "frontend"

Write-Host "Sports Hedge live canary stop completed. Local processes stopped. Venue orders already submitted are not claimed cancelled. Logs retained under logs\."
exit 0
