# Sports Hedge Phase 6 — read-only PAPER scanner soak (Windows)
# Observes a running paper backend. Does not place, cancel, or sign venue orders.
# Does not POST /paper/collect or otherwise start discovery/pricing work.

param(
    [int]$DurationSeconds = 720,
    [int]$IntervalSeconds = 15,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
if (-not (Test-Path $Logs)) {
    New-Item -ItemType Directory -Path $Logs | Out-Null
}

$Python = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Python venv not found at backend\.venv. Create it with:" -ForegroundColor Red
    Write-Host "cd backend"
    Write-Host "python -m venv .venv"
    Write-Host ".\.venv\Scripts\python -m pip install -e `".[dev]`""
    exit 1
}

Write-Host "PAPER / READ-ONLY scanner soak (Issue #350 Phase 6)"
Write-Host "This observer never places, cancels, or signs venue orders."
Write-Host "Base URL: $BaseUrl"
Write-Host "Duration: $DurationSeconds seconds (default 720 = 12 minutes)"

$Args = @(
    "-m", "sports_hedge.application.scanner_phase6",
    "--base-url", $BaseUrl,
    "--duration-seconds", "$DurationSeconds",
    "--interval-seconds", "$IntervalSeconds",
    "--data-kind", "owner_live_observation"
)
if ($Output -ne "") {
    $Args += @("--output", $Output)
}

Push-Location $Root
try {
    & $Python @Args
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host "Soak finished with hard-fail(s). Inspect the JSON report under logs\." -ForegroundColor Yellow
    exit $code
}

Write-Host "Soak observer finished. CI green is not demo-ready; this report is the owner-live gate."
exit 0
