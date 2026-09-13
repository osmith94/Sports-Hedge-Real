# Sports Hedge Step 9 — one-click paper demo (Windows)
# Starts FastAPI + Next.js hidden, waits for health, opens the operator console.
# Enables local paper autofill and the read-only live-refresh loop for this process.
# Does not enable live execution, wallet signing, or trading credentials.

$ErrorActionPreference = "Stop"

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

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
if (-not (Test-Path $Logs)) {
    New-Item -ItemType Directory -Path $Logs | Out-Null
}

$BackendPidFile = Join-Path $Logs "demo-backend.pid"
$FrontendPidFile = Join-Path $Logs "demo-frontend.pid"
$BackendLog = Join-Path $Logs "demo-backend.out.log"
$BackendErr = Join-Path $Logs "demo-backend.err.log"
$FrontendLog = Join-Path $Logs "demo-frontend.out.log"
$FrontendErr = Join-Path $Logs "demo-frontend.err.log"
$BackendHealth = "http://127.0.0.1:8000/health"
$FrontendHealth = "http://127.0.0.1:3000"
$DemoUrl = "http://127.0.0.1:3000/"

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
function Write-DemoPidIdentity {
    param(
        [string]$PidFile,
        [System.Diagnostics.Process]$Process,
        [string[]]$CommandTokens
    )
    $payload = @{
        pid = $Process.Id
        path = $Process.Path
        name = $Process.ProcessName
        command_tokens = @($CommandTokens)
    }
    ($payload | ConvertTo-Json -Compress) | Set-Content -Path $PidFile -Encoding utf8
}

$env:SPORTS_HEDGE_MODE = "paper"
$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"
$env:PAPER_AUTOFILL_ENABLED = "true"
$env:PAPER_LIVE_REFRESH_ENABLED = "true"
$env:NEXT_PUBLIC_SPORTS_HEDGE_API_URL = "http://127.0.0.1:8000"

$backendAlready = Test-HttpOk $BackendHealth
$frontendAlready = Test-HttpOk $FrontendHealth

if (-not $backendAlready) {
    $backendDir = Join-Path $Root "backend"
    $backend = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "sports_hedge.api.main:app",
        "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr -PassThru
    Write-DemoPidIdentity -PidFile $BackendPidFile -Process $backend -CommandTokens @(
        "uvicorn",
        "sports_hedge.api.main:app"
    )
}

if (-not $frontendAlready) {
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
    )
}

Wait-HttpOk -Url $BackendHealth -Label "Sports Hedge backend"
Wait-HttpOk -Url $FrontendHealth -Label "Sports Hedge operator console"

try {
    Start-Process $DemoUrl | Out-Null
} catch {
    Show-StartupError "Backend and frontend are up, but the browser could not be opened at $DemoUrl"
}

Write-Host "Sports Hedge paper demo is running."
Write-Host "Operator console: $DemoUrl"
Write-Host "PAPER MODE. execution_enabled=false. PAPER_AUTOFILL_ENABLED=true and PAPER_LIVE_REFRESH_ENABLED=true for this local demo only."
Write-Host "Logs: $Logs"
exit 0
