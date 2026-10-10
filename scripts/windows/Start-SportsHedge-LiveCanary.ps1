# Sports Hedge Wave 40 — explicit opt-in LIVE CANARY launcher.
# Separate from Start-SportsHedge-Real.ps1 (dry-run, execution OFF).
# Requires an interactive confirmation phrase. Does not accept an unattended toggle.
# Caps (£3 opening / £5 session / one opportunity) are enforced in the backend
# and are not enlarged by this script.
#
# This script may set SPORTS_HEDGE_EXECUTION_ENABLED=true only after the operator
# types ARM LIVE CANARY. It does not place orders by itself, print secrets, or
# start a VPN/proxy. PAPER_AUTOFILL_ENABLED stays false.
#
# PID files are live-canary-*.pid. A Real dry-run occupant of 8000/3000 is refused,
# not killed. Unresolved live-canary identity is reused only when SHA/repo match.

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
            "Sports Hedge live canary failed",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        # Headless fallback: caller .bat pauses on non-zero exit.
    }
    exit 1
}

function Get-CanaryHealthResponse {
    param([string]$Url)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 2
    } catch {
        return $null
    }
}

function Test-LiveCanaryHealthGate {
    param($Health)
    if ($null -eq $Health) {
        return $false
    }
    if ([string]$Health.mode -ne "real") {
        return $false
    }
    if ($Health.execution_enabled -ne $true) {
        return $false
    }
    if ($Health.paper_autofill_enabled -ne $false) {
        return $false
    }
    if ($null -eq $Health.live_canary -or $Health.live_canary.enabled -ne $true) {
        return $false
    }
    if ($Health.live_canary.operator_confirmed -ne $true) {
        return $false
    }
    if ($null -eq $Health.live_refresh -or $Health.live_refresh.server_loop_enabled -ne $true) {
        return $false
    }
    return $true
}

function Format-LiveCanaryHealthGate {
    param($Health)
    if ($null -eq $Health) {
        return "no /health response"
    }
    $loop = $null
    if ($null -ne $Health.live_refresh) {
        $loop = $Health.live_refresh.server_loop_enabled
    }
    $canary = $null
    if ($null -ne $Health.live_canary) {
        $canary = $Health.live_canary.enabled
    }
    return "mode=$($Health.mode) execution_enabled=$($Health.execution_enabled) live_canary.enabled=$canary paper_autofill_enabled=$($Health.paper_autofill_enabled) live_refresh.server_loop_enabled=$loop"
}

function Wait-CanaryHealthResponse {
    param(
        [string]$Url,
        [int]$Seconds = 90
    )
    for ($i = 0; $i -lt $Seconds; $i++) {
        $health = Get-CanaryHealthResponse -Url $Url
        if ($null -ne $health) {
            return $health
        }
        Start-Sleep -Seconds 1
    }
    return $null
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
if (-not (Test-Path $Logs)) {
    New-Item -ItemType Directory -Path $Logs | Out-Null
}

$BackendPidFile = Join-Path $Logs "live-canary-backend.pid"
$FrontendPidFile = Join-Path $Logs "live-canary-frontend.pid"
$BackendLog = Join-Path $Logs "live-canary-backend.out.log"
$BackendErr = Join-Path $Logs "live-canary-backend.err.log"
$FrontendLog = Join-Path $Logs "live-canary-frontend.out.log"
$FrontendErr = Join-Path $Logs "live-canary-frontend.err.log"
$AuthFile = Join-Path $Logs "live-canary-authorization.json"
$BackendHealth = "http://127.0.0.1:8000/health"
$FrontendReady = "http://127.0.0.1:3000/api/desktop/status"
$ConsoleUrl = "http://127.0.0.1:3000/"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Red
Write-Host " LIVE REAL-MONEY ORDERS — WAVE 40 CANARY" -ForegroundColor Red
Write-Host " This is NOT the dry-run Real launcher." -ForegroundColor Red
Write-Host " First session: one opportunity, £3 opening both legs, £5 max." -ForegroundColor Red
Write-Host " Native minima that exceed £3 are NOT READY. Stakes are not raised." -ForegroundColor Red
Write-Host " No VPN/proxy. Current-IP Polymarket geoblock fails closed." -ForegroundColor Red
Write-Host " Transport /health.execution.live_execution_ready is not permission." -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Red
Write-Host ""

if (-not [Environment]::UserInteractive) {
    Show-StartupError "Live canary requires an interactive operator confirmation. Unattended start is refused."
}

$typed = Read-Host "Type ARM LIVE CANARY to continue (anything else aborts)"
if ($typed -cne "ARM LIVE CANARY") {
    Show-StartupError "Live canary aborted. Confirmation phrase did not match."
}

try {
    $Git = Get-RepoGitIdentity -RepoRoot $Root
} catch {
    Show-StartupError "Could not read Git HEAD from $Root. The live canary launcher requires git. $_"
}

$SessionId = [guid]::NewGuid().ToString()
$auth = @{
    session_id = $SessionId
    confirmed_at = [DateTime]::UtcNow.ToString("o")
    phrase = "ARM LIVE CANARY"
} | ConvertTo-Json
Set-Content -Path $AuthFile -Value $auth -Encoding utf8

$Python = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Show-StartupError "Python venv not found at backend\.venv."
}

$Npm = Get-Command npm -ErrorAction SilentlyContinue
if ($null -eq $Npm) {
    Show-StartupError "npm was not found on PATH."
}

$FrontendDir = Join-Path $Root "frontend"
if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Show-StartupError "frontend\node_modules missing. Run npm install in frontend\ first."
}

# Canary process environment. Dry-run Real launcher is unchanged and stays OFF.
$env:SPORTS_HEDGE_MODE = "real"
$env:SPORTS_HEDGE_EXECUTION_ENABLED = "true"
$env:SPORTS_HEDGE_LIVE_CANARY_ENABLED = "true"
$env:SPORTS_HEDGE_LIVE_CANARY_OPERATOR_CONFIRMED = "true"
$env:SPORTS_HEDGE_LIVE_CANARY_SESSION_ID = $SessionId
$env:SPORTS_HEDGE_LIVE_CANARY_AUTHORIZATION_PATH = $AuthFile
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
if (Test-Path $LegacyBackendDotEnv) {
    Write-Host "WARNING: ignoring leftover $LegacyBackendDotEnv." -ForegroundColor Yellow
}

function Get-CanaryServiceDecision {
    param(
        [string]$Label,
        [int]$Port,
        [string]$PidFile
    )
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

function Invoke-CanaryOwnedService {
    param(
        [string]$Label,
        [int]$Port,
        [string]$PidFile,
        [scriptblock]$Starter,
        [int]$AwaitSeconds = 90
    )
    $decision = Get-CanaryServiceDecision -Label $Label -Port $Port -PidFile $PidFile
    if ($decision.action -eq "await") {
        $ownedPid = $decision.identity.pid
        Write-Host "${Label}: owned live-canary PID $ownedPid for SHA $($Git.sha) is still starting; waiting for port $Port"
        for ($i = 0; $i -lt $AwaitSeconds -and $decision.action -eq "await"; $i++) {
            Start-Sleep -Seconds 1
            $decision = Get-CanaryServiceDecision -Label $Label -Port $Port -PidFile $PidFile
        }
        if ($decision.action -eq "await") {
            Show-StartupError "$Label PID $ownedPid has not bound port $Port. Refusing a second copy. Run Stop-SportsHedge-LiveCanary.bat."
        }
        if ($decision.action -eq "start") {
            Write-Host "${Label}: owned PID $ownedPid exited before binding port $Port; port $Port confirmed free"
        }
    }
    $action = $decision.action
    $identity = $decision.identity
    if ($action -eq "reuse") {
        Write-Host "${Label}: reused existing Sports Hedge live-canary process for SHA $($Git.sha)"
        return $action
    }
    if ($action -eq "conflict") {
        Show-StartupError "$Label port $Port is already in use (listener PID $(@($decision.listener_pids) -join ', ')) but is not this live-canary launcher for SHA $($Git.sha). A Real dry-run or unrelated process is not reused or killed."
    }
    if ($action -eq "restart") {
        Write-Host "${Label}: restart required; recorded SHA $($identity.git_head) != current $($Git.sha)"
        Stop-DemoPid -PidFile $PidFile -Label $Label
        Wait-DemoPortGone -Port $Port -Label $Label
    }
    & $Starter | Out-Null
    if ($action -eq "restart") {
        Write-Host "${Label}: restarted owned live-canary process because recorded SHA $($identity.git_head) != current $($Git.sha)"
    } else {
        Write-Host "${Label}: started live canary for SHA $($Git.sha)"
    }
    return $action
}

$StartBackend = {
    $backend = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "sports_hedge.api.main:app",
        "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr -PassThru
    Write-DemoPidIdentity -PidFile $BackendPidFile -Process $backend -CommandTokens @(
        "uvicorn",
        "sports_hedge.api.main:app"
    ) -GitHead $Git.sha -RepoRoot $Root
}

$backendAction = Invoke-CanaryOwnedService -Label "backend" -Port 8000 -PidFile $BackendPidFile -Starter $StartBackend

Invoke-CanaryOwnedService -Label "frontend" -Port 3000 -PidFile $FrontendPidFile -Starter {
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npmCmd) {
        Show-StartupError "npm.cmd was not found on PATH."
    }
    $frontend = Start-Process -FilePath $npmCmd.Source -ArgumentList @("run", "dev", "--", "-H", "127.0.0.1", "-p", "3000") -WorkingDirectory $FrontendDir -WindowStyle Hidden -RedirectStandardOutput $FrontendLog -RedirectStandardError $FrontendErr -PassThru
    Write-DemoPidIdentity -PidFile $FrontendPidFile -Process $frontend -CommandTokens @(
        "run",
        "dev",
        "127.0.0.1",
        "3000"
    ) -GitHead $Git.sha -RepoRoot $Root
} | Out-Null

$health = Wait-CanaryHealthResponse -Url $BackendHealth -Seconds 90
if (-not (Test-LiveCanaryHealthGate -Health $health) -and $backendAction -eq "reuse") {
    Write-Host "backend: owned live-canary process failed the canary health gate ($(Format-LiveCanaryHealthGate -Health $health)). Stopping it after port 8000 is free."
    Stop-DemoPid -PidFile $BackendPidFile -Label "backend"
    Wait-DemoPortGone -Port 8000 -Label "backend"
    & $StartBackend | Out-Null
    $health = Wait-CanaryHealthResponse -Url $BackendHealth -Seconds 90
}
if (-not (Test-LiveCanaryHealthGate -Health $health)) {
    Show-StartupError "Live canary /health did not prove mode=real, execution_enabled=true, live_canary.enabled=true, paper_autofill_enabled=false, and scanner loop on. Observed $(Format-LiveCanaryHealthGate -Health $health). This gate does not treat execution.live_execution_ready as permission."
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
    Show-StartupError "Live canary operator console did not become ready at $FrontendReady."
}

try {
    Start-Process $ConsoleUrl | Out-Null
} catch {
    Show-StartupError "Backend and frontend are up, but the browser could not be opened at $ConsoleUrl"
}

Write-Host "LIVE CANARY"
Write-Host "LIVE REAL-MONEY ORDERS ARMED FOR THIS PROCESS ONLY"
Write-Host "PAPER_AUTOFILL_ENABLED=false (not AUTO PAPER CAPTURE)"
Write-Host "Session id: $SessionId"
Write-Host "Operator console: $ConsoleUrl"
Write-Host "HALT NEW ORDERS: POST http://127.0.0.1:8000/live-canary/halt-new-orders"
Write-Host "Halt does not cancel orders already submitted. Check venue histories for unknown ACK."
Write-Host "Serving Git SHA: $($Git.sha)"
Write-Host "This launcher does not merge, deploy, or touch credentials."
exit 0
