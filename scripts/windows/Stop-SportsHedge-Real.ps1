# Stop processes started by Start-SportsHedge-Real.ps1.
# Never force-kill a reused PID. Identity JSON (pid + path + command tokens)
# must match the live process command/path before Stop-Process.
# Owned frontend npm wrappers also stop verified Next.js descendants discovered
# via Win32_Process.ParentProcessId. Unrelated Node and Python processes are not killed.
# Uses the Real PID files only. Demo PID files are left untouched.
# Decision contract matches sports_hedge.application.demo_launcher_pid.

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
Stop-DemoPid -PidFile (Join-Path $Logs "real-backend.pid") -Label "backend"
Stop-DemoPid -PidFile (Join-Path $Logs "real-frontend.pid") -Label "frontend"

Write-Host "Sports Hedge Real stop completed. Logs retained under logs\."
exit 0
