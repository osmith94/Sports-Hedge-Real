# One-command Windows demo refresh onto latest owner-live.
# Stop owned process tree → verify ports 3000/8000 are gone → fetch/switch/pull
# owner-live → clear frontend\.next → print branch/SHA → Start.
# Existing Stop/Start scripts remain the source of truth for process identity
# and paper-only startup. Never broad-kill Node. Never delete node_modules.
# Does not enable live execution, wallet signing, or trading credentials.

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Show-RefreshError {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Invoke-DemoLauncherScript {
    param(
        [string]$ScriptName,
        [string]$FailureMessage
    )
    $scriptPath = Join-Path $PSScriptRoot $ScriptName
    if (-not (Test-Path $scriptPath)) {
        Show-RefreshError "Missing launcher script: $scriptPath"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath
    if ($LASTEXITCODE -ne 0) {
        Show-RefreshError $FailureMessage
    }
}

function Invoke-RepoGit {
    param(
        [string[]]$GitArgs,
        [string]$FailureMessage
    )
    & git -C $Root @GitArgs
    if ($LASTEXITCODE -ne 0) {
        Show-RefreshError $FailureMessage
    }
}

$Root = Get-RepoRoot
if (-not (Test-Path (Join-Path $Root ".git"))) {
    Show-RefreshError "Could not resolve the Sports Hedge repository root from $PSScriptRoot."
}

$Logs = Join-Path $Root "logs"
$FrontendPidFile = Join-Path $Logs "demo-frontend.pid"
$ownedFrontendTree = @()
if (Test-Path $FrontendPidFile) {
    $frontendIdentity = ConvertTo-DemoIdentity -Raw (Get-Content $FrontendPidFile -Raw -ErrorAction SilentlyContinue)
    if ($null -ne $frontendIdentity) {
        $frontendLive = Get-DemoLiveProcess -ProcId $frontendIdentity.pid
        if ((Test-DemoPidOwned -Identity $frontendIdentity -Live $frontendLive) -eq "stop") {
            $ownedFrontendTree = @(Get-DemoOwnedTreePids -RootPid $frontendIdentity.pid)
        }
    }
}

Invoke-DemoLauncherScript -ScriptName "Stop-SportsHedge-Demo.ps1" -FailureMessage "Stop-SportsHedge-Demo.ps1 failed. frontend\.next was not deleted."

foreach ($procId in @($ownedFrontendTree)) {
    $still = Get-DemoLiveProcess -ProcId ([int]$procId)
    if ($still.present) {
        Show-RefreshError "Verified frontend descendant PID $procId is still running after stop. Refusing to delete frontend\.next."
    }
}

if (Test-DemoPortListening -Port 3000) {
    Show-RefreshError "Frontend port 3000 is still listening after stop. Refusing to delete frontend\.next rather than killing an unexpected occupant of the port."
}
if (Test-DemoPortListening -Port 8000) {
    Show-RefreshError "Backend port 8000 is still listening after stop. Refusing to delete frontend\.next rather than killing an unexpected occupant of the port."
}

Invoke-RepoGit -GitArgs @("fetch", "origin") -FailureMessage "git fetch origin failed. frontend\.next was not deleted."
Invoke-RepoGit -GitArgs @("switch", "owner-live") -FailureMessage "git switch owner-live failed. frontend\.next was not deleted."
Invoke-RepoGit -GitArgs @("pull", "--ff-only") -FailureMessage "git pull --ff-only failed. frontend\.next was not deleted."

$NextDir = Join-Path $Root "frontend\.next"
if (Test-Path $NextDir) {
    Remove-Item -LiteralPath $NextDir -Recurse -Force
    Write-Host "Cleared $NextDir"
}

try {
    $Git = Get-RepoGitIdentity -RepoRoot $Root
} catch {
    Show-RefreshError "Could not read Git HEAD from $Root after pull. $_"
}
Write-Host "Current branch: $($Git.branch)"
Write-Host "Current SHA: $($Git.sha)"

Invoke-DemoLauncherScript -ScriptName "Start-SportsHedge-Demo.ps1" -FailureMessage "Start-SportsHedge-Demo.ps1 failed after refresh checkout $($Git.sha)."
exit 0
