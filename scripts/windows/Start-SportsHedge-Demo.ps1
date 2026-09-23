# Sports Hedge Step 9 — one-click paper demo (Windows)
# Starts FastAPI + Next.js hidden, waits for health, opens the operator console.
# Enables the read-only live-refresh loop for this process. Autofill stays off so
# the operator can preview and confirm a concrete size (example £10) before OPEN.
# Enables the existing accounting/FX scheduler so a fresh FX DB bootstraps the
# latest published ECB USD close (weekend/holiday carry-forward) without waiting
# for the 16:15 UK window. Does not enable live execution, wallet signing, or
# trading credentials.
# Canonical local dotenv is repository-root .env. backend\.env is ignored.
# Reuses a healthy backend/frontend only when the launcher-owned PID identity,
# command, repo root, and Git HEAD match this checkout. A different HEAD
# restarts that owned process. An unrelated occupant of 8000/3000 is refused
# rather than killed.

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
            "Sports Hedge demo failed",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        # Headless fallback: caller .bat pauses on non-zero exit.
    }
    exit 1
}

function Test-HttpOk {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$Seconds = 90,
        [string]$Label
    )
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-HttpOk $Url) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    Show-StartupError "$Label did not become healthy at $Url within $Seconds seconds. Inspect logs under logs\."
}

function Wait-HttpGone {
    param(
        [string]$Url,
        [int]$Seconds = 15,
        [string]$Label
    )
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (-not (Test-HttpOk $Url)) {
            return
        }
        Start-Sleep -Seconds 1
    }
    Show-StartupError "$Label is still healthy at $Url after stopping the launcher-owned process. Refusing to kill an unexpected occupant of the port."
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
if (-not (Test-Path $Logs)) {
    New-Item -ItemType Directory -Path $Logs | Out-Null
}

$BackendPidFile = Join-Path $Logs "demo-backend.pid"
$FrontendPidFile = Join-Path $Logs "demo-frontend.pid"
$BackendLog = Join-Path $Logs "demo-backend.out.log"
# faulthandler tracebacks, unhandled asyncio exceptions, and backend_fatal
# records are written to this process stderr. Keep RedirectStandardError on
# $BackendErr so they land in logs\demo-backend.err.log.
$BackendErr = Join-Path $Logs "demo-backend.err.log"
$FrontendLog = Join-Path $Logs "demo-frontend.out.log"
$FrontendErr = Join-Path $Logs "demo-frontend.err.log"
$BackendHealth = "http://127.0.0.1:8000/health"
$FrontendHealth = "http://127.0.0.1:3000"
$DemoUrl = "http://127.0.0.1:3000/"

try {
    $Git = Get-RepoGitIdentity -RepoRoot $Root
} catch {
    Show-StartupError "Could not read Git HEAD from $Root. The demo launcher requires git to record the serving checkout. $_"
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

# Paper-only demo environment. Do not inject trading, wallet, or execution secrets.
$env:SPORTS_HEDGE_MODE = "paper"
$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"
$env:PAPER_AUTOFILL_ENABLED = "true"
$env:PAPER_AUTO_UNWIND_ENABLED = "true"
$env:PAPER_LIVE_REFRESH_ENABLED = "true"
$env:ACCOUNTING_SCHEDULE_ENABLED = "true"
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

function Invoke-DemoOwnedService {
    param(
        [string]$Label,
        [string]$HealthUrl,
        [string]$PidFile,
        [scriptblock]$Starter
    )
    $healthOk = Test-HttpOk $HealthUrl
    $identity = $null
    if (Test-Path $PidFile) {
        $identity = ConvertTo-DemoIdentity -Raw (Get-Content $PidFile -Raw -ErrorAction SilentlyContinue)
    }
    $live = $null
    if ($null -ne $identity -and $null -ne $identity.pid) {
        $live = Get-DemoLiveProcess -ProcId $identity.pid
    }
    $action = Get-DemoStartAction -HealthOk $healthOk -Identity $identity -Live $live -CurrentGitHead $Git.sha -CurrentRepoRoot $Root
    if ($action -eq "reuse") {
        Write-Host "${Label}: reused existing Sports Hedge process for SHA $($Git.sha)"
        return
    }
    if ($action -eq "conflict") {
        Show-StartupError "$Label is healthy at $HealthUrl but is not a Sports Hedge launcher process for this checkout (SHA $($Git.sha)). Refusing to reuse or kill the unrelated process occupying the port."
    }
    if ($action -eq "restart") {
        Write-Host "${Label}: restart required; recorded SHA $($identity.git_head) != current $($Git.sha)"
        Stop-DemoPid -PidFile $PidFile -Label $Label
        Wait-HttpGone -Url $HealthUrl -Label $Label
    }
    & $Starter
    if ($action -eq "restart") {
        Write-Host "${Label}: restarted owned process because recorded SHA $($identity.git_head) != current $($Git.sha)"
    } else {
        Write-Host "${Label}: started for SHA $($Git.sha)"
    }
}

Invoke-DemoOwnedService -Label "backend" -HealthUrl $BackendHealth -PidFile $BackendPidFile -Starter {
    $backend = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "sports_hedge.api.main:app",
        "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr -PassThru
    Write-DemoPidIdentity -PidFile $BackendPidFile -Process $backend -CommandTokens @(
        "uvicorn",
        "sports_hedge.api.main:app"
    ) -GitHead $Git.sha -RepoRoot $Root
}

Invoke-DemoOwnedService -Label "frontend" -HealthUrl $FrontendHealth -PidFile $FrontendPidFile -Starter {
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
}

Wait-HttpOk -Url $BackendHealth -Label "Sports Hedge backend" | Out-Null
Wait-HttpOk -Url $FrontendHealth -Label "Sports Hedge operator console" | Out-Null

try {
    Start-Process $DemoUrl | Out-Null
} catch {
    Show-StartupError "Backend and frontend are up, but the browser could not be opened at $DemoUrl"
}

Write-Host "Sports Hedge paper demo is running."
Write-Host "Operator console: $DemoUrl"
Write-Host "Serving Git SHA: $($Git.sha) ($($Git.branch))"
Write-Host "Build identity: GET http://127.0.0.1:8000/build-info"
Write-Host "PAPER MODE. execution_enabled=false. PAPER_AUTOFILL_ENABLED=true (AUTO PAPER CAPTURE ON for qualifying LIVE_PAPER only; allocator-sized; no venue orders). PAPER_AUTO_UNWIND_ENABLED=true (AUTO PAPER POSITION MANAGEMENT ON; paper-only; two-scan fail-closed confirmation; no live execution; no automatic authoritative settlement). PAPER_LIVE_REFRESH_ENABLED=true and ACCOUNTING_SCHEDULE_ENABLED=true for this local demo only (ECB USD bootstrap + daily 16:15 UK refresh)."
Write-Host "Canonical local dotenv remains $CanonicalDotEnv; backend\.env is not active configuration."
Write-Host "Logs: $Logs"
exit 0
