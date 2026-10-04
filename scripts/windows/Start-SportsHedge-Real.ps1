# Sports Hedge Real — one-command Windows dry-run launcher.
# Starts FastAPI + Next.js hidden, then refuses to claim success until GET /health
# reports mode=real, execution_enabled=false, and live_refresh.server_loop_enabled=true.
#
# Required process environment (quoted assignments in this file, never
# Start-Process -Command, which strips quotes and runs the values as commands):
#   SPORTS_HEDGE_MODE=real
#   SPORTS_HEDGE_EXECUTION_ENABLED=false
#   PAPER_LIVE_REFRESH_ENABLED=true
# PAPER_LIVE_REFRESH_ENABLED is the legacy-named scanner-loop switch. It does
# not select paper mode. Autofill, auto-unwind, and the accounting schedule
# stay explicitly false. Persisted operator pauses are reported, never cleared.
#
# Canonical local dotenv is repository-root .env. backend\.env is ignored.
# Process environment overrides dotenv. This script does not print secret values.
# Reuses a listening backend/frontend only when the launcher-owned PID identity,
# command, repo root, and Git HEAD match this checkout and the port listener is
# that PID or a verified descendant. Occupancy comes from the OS listener, never
# from HTTP. An owned current-HEAD process that has not bound its port yet is
# awaited, not duplicated. A different HEAD restarts that owned process. A reused
# owned backend that fails the Real health gate is stopped and replaced once,
# and only after the port is confirmed free, so a stale listener cannot raise
# WinError 10048. An unrelated occupant of 8000/3000 is refused rather than killed.

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Show-StartupError {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        [System.Windows.Forms.MessageBox]::Show(
            $Message,
            "Sports Hedge Real failed",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        # Headless fallback: caller .bat pauses on non-zero exit.
    }
    exit 1
}

function Get-RealHealthResponse {
    param([string]$Url)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 2
    } catch {
        return $null
    }
}

function Test-RealHealthGate {
    param($Health)
    if ($null -eq $Health) {
        return $false
    }
    if ([string]$Health.mode -ne "real") {
        return $false
    }
    if ($Health.execution_enabled -ne $false) {
        return $false
    }
    if ($null -eq $Health.live_refresh -or $Health.live_refresh.server_loop_enabled -ne $true) {
        return $false
    }
    return $true
}

function Format-RealHealthGate {
    param($Health)
    if ($null -eq $Health) {
        return "no /health response"
    }
    $loop = $null
    if ($null -ne $Health.live_refresh) {
        $loop = $Health.live_refresh.server_loop_enabled
    }
    return "mode=$($Health.mode) execution_enabled=$($Health.execution_enabled) live_refresh.server_loop_enabled=$loop"
}

function Wait-RealHealthResponse {
    param(
        [string]$Url,
        [int]$Seconds = 90
    )
    for ($i = 0; $i -lt $Seconds; $i++) {
        $health = Get-RealHealthResponse -Url $Url
        if ($null -ne $health) {
            return $health
        }
        Start-Sleep -Seconds 1
    }
    return $null
}

function Write-RealOperatorPauseReport {
    param($Health)
    $liveRefresh = $Health.live_refresh
    Write-Host "scanner_stopped=$($liveRefresh.scanner_stopped)"
    Write-Host "universe_scans_paused=$($liveRefresh.universe_scans_paused)"
    Write-Host "background_pricing_paused=$($liveRefresh.background_pricing_paused)"
    Write-Host "settlement_scans_paused=$($liveRefresh.settlement_scans_paused)"
    if ($liveRefresh.scanner_stopped -eq $true) {
        Write-Host "SCANNER master stop is persisted as paused by operator." -ForegroundColor Yellow
    }
    if ($liveRefresh.universe_scans_paused -eq $true) {
        Write-Host "UNIVERSE schedule is persisted as paused by operator." -ForegroundColor Yellow
    }
    if ($liveRefresh.background_pricing_paused -eq $true) {
        Write-Host "BACKGROUND pricing is persisted as paused by operator." -ForegroundColor Yellow
    }
    if ($liveRefresh.settlement_scans_paused -eq $true) {
        Write-Host "SETTLEMENT scans are persisted as paused by operator." -ForegroundColor Yellow
    }
    Write-Host "Persisted operator pauses were reported only. This launcher does not resume scanner, universe schedule, background pricing, or settlement."
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
if (-not (Test-Path $Logs)) {
    New-Item -ItemType Directory -Path $Logs | Out-Null
}

$BackendPidFile = Join-Path $Logs "real-backend.pid"
$FrontendPidFile = Join-Path $Logs "real-frontend.pid"
$BackendLog = Join-Path $Logs "real-backend.out.log"
# faulthandler tracebacks, unhandled asyncio exceptions, and backend_fatal
# records are written to this process stderr. Keep RedirectStandardError on
# $BackendErr so they land in logs\real-backend.err.log.
$BackendErr = Join-Path $Logs "real-backend.err.log"
$FrontendLog = Join-Path $Logs "real-frontend.out.log"
$FrontendErr = Join-Path $Logs "real-frontend.err.log"
$BackendHealth = "http://127.0.0.1:8000/health"
# Startup readiness gate: lightweight route, never the expensive operator homepage.
$FrontendReady = "http://127.0.0.1:3000/api/desktop/status"
$ConsoleUrl = "http://127.0.0.1:3000/"

try {
    $Git = Get-RepoGitIdentity -RepoRoot $Root
} catch {
    Show-StartupError "Could not read Git HEAD from $Root. The Real launcher requires git to record the serving checkout. $_"
}

Write-Host "Current branch: $($Git.branch)"
Write-Host "Current SHA: $($Git.sha)"

$Python = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Show-StartupError "Python venv not found at backend\.venv. Create it with:`ncd backend`npython -m venv .venv`n.venv\Scripts\python -m pip install -e `".[dev]`""
}

$Npm = Get-Command npm -ErrorAction SilentlyContinue
if ($null -eq $Npm) {
    Show-StartupError "npm was not found on PATH. Install Node.js, then run npm install in frontend\."
}

$FrontendDir = Join-Path $Root "frontend"
$NodeModules = Join-Path $FrontendDir "node_modules"
if (-not (Test-Path $NodeModules)) {
    Write-Host "Installing frontend dependencies (one-time)..."
    Push-Location $FrontendDir
    try {
        & npm.cmd install
        if ($LASTEXITCODE -ne 0) {
            Show-StartupError "npm install failed in frontend\. See the console output above."
        }
    } finally {
        Pop-Location
    }
}

# Real dry-run environment. Execution stays off. Scanner loop stays on.
# Do not inject trading, wallet, or execution secrets. Do not enable autofill,
# auto-unwind, settlement, or the accounting schedule.
$env:SPORTS_HEDGE_MODE = "real"
$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"
$env:PAPER_LIVE_REFRESH_ENABLED = "true"
$env:PAPER_AUTOFILL_ENABLED = "false"
$env:PAPER_AUTO_UNWIND_ENABLED = "false"
$env:ACCOUNTING_SCHEDULE_ENABLED = "false"
$env:NEXT_PUBLIC_SPORTS_HEDGE_API_URL = "http://127.0.0.1:8000"
$env:SPORTS_HEDGE_GIT_SHA = $Git.sha
$env:SPORTS_HEDGE_GIT_BRANCH = $Git.branch
$env:SPORTS_HEDGE_REPO_ROOT = $Root

$backendDir = Join-Path $Root "backend"
$CanonicalDotEnv = Join-Path $Root ".env"
$LegacyBackendDotEnv = Join-Path $backendDir ".env"
Write-Host "Canonical local dotenv: $CanonicalDotEnv"
Write-Host "backend\.env is ignored even though the backend process working directory is backend\."
if (Test-Path $LegacyBackendDotEnv) {
    Write-Host "WARNING: ignoring leftover $LegacyBackendDotEnv. Use $CanonicalDotEnv and remove the leftover file so it is not mistaken for active configuration." -ForegroundColor Yellow
}

function Get-RealServiceDecision {
    param(
        [string]$Label,
        [int]$Port,
        [string]$PidFile
    )
    # Occupancy and identity are deliberately separate from HTTP readiness.
    # A slow page or a live /health response must never make an occupied port look empty.
    $listenerPids = @(Get-DemoPortListenerPids -Port $Port)
    $portListening = $listenerPids.Count -gt 0
    $identity = $null
    if (Test-Path $PidFile) {
        $identity = ConvertTo-DemoIdentity -Raw (Get-Content $PidFile -Raw -ErrorAction SilentlyContinue)
    }
    $live = $null
    if ($null -ne $identity -and $null -ne $identity.pid) {
        $live = Get-DemoLiveProcess -ProcId $identity.pid
    }
    $listenerOwned = $false
    if ($portListening -and $null -ne $identity) {
        $listenerOwned = Test-DemoListenerOwned -Identity $identity -Label $Label -ListenerPids $listenerPids
    }
    $action = Get-DemoStartAction -PortListening $portListening -ListenerOwned $listenerOwned -Identity $identity -Live $live -CurrentGitHead $Git.sha -CurrentRepoRoot $Root
    return @{
        action = $action
        identity = $identity
        listener_pids = $listenerPids
    }
}

function Invoke-RealOwnedService {
    param(
        [string]$Label,
        [int]$Port,
        [string]$PidFile,
        [scriptblock]$Starter,
        [int]$AwaitSeconds = 90
    )
    $decision = Get-RealServiceDecision -Label $Label -Port $Port -PidFile $PidFile
    if ($decision.action -eq "await") {
        $ownedPid = $decision.identity.pid
        Write-Host "${Label}: owned Sports Hedge Real PID $ownedPid for SHA $($Git.sha) is still starting; waiting for port $Port instead of launching a second copy"
        for ($i = 0; $i -lt $AwaitSeconds -and $decision.action -eq "await"; $i++) {
            Start-Sleep -Seconds 1
            $decision = Get-RealServiceDecision -Label $Label -Port $Port -PidFile $PidFile
        }
        if ($decision.action -eq "await") {
            Show-StartupError "$Label PID $ownedPid is a Sports Hedge Real launcher process for this checkout but has not bound port $Port within $AwaitSeconds seconds. Refusing to start a second copy. Inspect logs under logs\, or run Stop-SportsHedge-Real.bat and retry."
        }
        if ($decision.action -eq "start") {
            Write-Host "${Label}: owned PID $ownedPid exited before binding port $Port; port $Port confirmed free"
        }
    }
    $action = $decision.action
    $identity = $decision.identity
    if ($action -eq "reuse") {
        Write-Host "${Label}: reused existing Sports Hedge Real process for SHA $($Git.sha)"
        return $action
    }
    if ($action -eq "conflict") {
        Show-StartupError "$Label port $Port is already in use (listener PID $(@($decision.listener_pids) -join ', ')) but is not a Sports Hedge Real launcher process for this checkout (SHA $($Git.sha)). Refusing to reuse or kill the unrelated process occupying the port."
    }
    if ($action -eq "restart") {
        Write-Host "${Label}: restart required; recorded SHA $($identity.git_head) != current $($Git.sha)"
        Stop-DemoPid -PidFile $PidFile -Label $Label
        # Stop-DemoPid returns early without a port wait if the owned PID has
        # already exited; never start into a port something else still holds.
        Wait-DemoPortGone -Port $Port -Label $Label
    }
    & $Starter | Out-Null
    if ($action -eq "restart") {
        Write-Host "${Label}: restarted owned process because recorded SHA $($identity.git_head) != current $($Git.sha)"
    } else {
        Write-Host "${Label}: started for SHA $($Git.sha)"
    }
    return $action
}

$StartBackend = {
    # -FilePath/-ArgumentList keeps each token quoted. Do not switch this to
    # Start-Process -Command: that path strips quotes around environment values.
    $backend = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "sports_hedge.api.main:app",
        "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr -PassThru
    Write-DemoPidIdentity -PidFile $BackendPidFile -Process $backend -CommandTokens @(
        "uvicorn",
        "sports_hedge.api.main:app"
    ) -GitHead $Git.sha -RepoRoot $Root
}

$backendAction = Invoke-RealOwnedService -Label "backend" -Port 8000 -PidFile $BackendPidFile -Starter $StartBackend

Invoke-RealOwnedService -Label "frontend" -Port 3000 -PidFile $FrontendPidFile -Starter {
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npmCmd) {
        Show-StartupError "npm.cmd was not found on PATH. Install Node.js, then retry."
    }
    $frontend = Start-Process -FilePath $npmCmd.Source -ArgumentList @("run", "dev", "--", "-H", "127.0.0.1", "-p", "3000") -WorkingDirectory $FrontendDir -WindowStyle Hidden -RedirectStandardOutput $FrontendLog -RedirectStandardError $FrontendErr -PassThru
    Write-DemoPidIdentity -PidFile $FrontendPidFile -Process $frontend -CommandTokens @(
        "run",
        "dev",
        "127.0.0.1",
        "3000"
    ) -GitHead $Git.sha -RepoRoot $Root
} | Out-Null

$health = Wait-RealHealthResponse -Url $BackendHealth -Seconds 90
if (-not (Test-RealHealthGate -Health $health) -and $backendAction -eq "reuse") {
    Write-Host "backend: launcher-owned process failed the Real health gate ($(Format-RealHealthGate -Health $health)). Stopping it and starting one replacement after port 8000 is free."
    Stop-DemoPid -PidFile $BackendPidFile -Label "backend"
    Wait-DemoPortGone -Port 8000 -Label "backend"
    & $StartBackend | Out-Null
    $health = Wait-RealHealthResponse -Url $BackendHealth -Seconds 90
}
if (-not (Test-RealHealthGate -Health $health)) {
    Show-StartupError "Sports Hedge Real backend /health did not prove mode=real, execution_enabled=false, and live_refresh.server_loop_enabled=true. Observed $(Format-RealHealthGate -Health $health). Branch $($Git.branch) SHA $($Git.sha). Refusing to report the launcher as running. Inspect logs under logs\."
}

$frontendReady = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $response = Invoke-WebRequest -Uri $FrontendReady -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
            $frontendReady = $true
            break
        }
    } catch {
        $frontendReady = $false
    }
    Start-Sleep -Seconds 1
}
if (-not $frontendReady) {
    Show-StartupError "Sports Hedge Real operator console did not become ready at $FrontendReady within 90 seconds. Inspect logs under logs\."
}

try {
    Start-Process $ConsoleUrl | Out-Null
} catch {
    Show-StartupError "Backend and frontend are up, but the browser could not be opened at $ConsoleUrl"
}

Write-Host "REAL MODE"
Write-Host "REAL SCANNER LOOP ENABLED"
Write-Host "LIVE ORDER EXECUTION DISABLED"
Write-Host "Sports Hedge Real is running."
Write-Host "Operator console: $ConsoleUrl"
Write-Host "Serving Git SHA: $($Git.sha)"
Write-Host "Serving Git branch: $($Git.branch)"
Write-Host "GET /health confirmed mode=real execution_enabled=false live_refresh.server_loop_enabled=true"
Write-RealOperatorPauseReport -Health $health
Write-Host "SPORTS_HEDGE_MODE=real SPORTS_HEDGE_EXECUTION_ENABLED=false PAPER_LIVE_REFRESH_ENABLED=true (legacy-named scanner loop; does not switch runtime into paper mode). PAPER_AUTOFILL_ENABLED=false PAPER_AUTO_UNWIND_ENABLED=false ACCOUNTING_SCHEDULE_ENABLED=false."
Write-Host "Canonical local dotenv remains $CanonicalDotEnv; backend\.env is not active configuration."
Write-Host "Logs: $Logs"
exit 0
