# Repeat the unchanged dedicated Windows event-loop liveness selection.
#
# This is the command in .github/workflows/backend-ci.yml job
# windows-event-loop-liveness. It does not change thresholds, skip tests, or
# disable GC. Run it on a Windows machine with Python 3.12. A Linux run is not
# this job.
#
# Alternating two checkouts, five times each:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\windows_liveness_matrix.ps1 `
#     -FirstRoot C:\src\Sports-Hedge-Real-main `
#     -FirstLabel main `
#     -SecondRoot C:\src\Sports-Hedge-Real-pr17 `
#     -SecondLabel pr `
#     -Repeats 5
#
# Check out main at b324d5a for -FirstRoot and the PR head for -SecondRoot.
# Install backend deps in each checkout first:
#
#   python -m pip install -e ".\backend[dev]"
#
# Logs land in -LogDir. Each file ends with EXIT:<code>.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FirstRoot,
    [string]$FirstLabel = "main",
    [Parameter(Mandatory = $true)]
    [string]$SecondRoot,
    [string]$SecondLabel = "pr",
    [int]$Repeats = 5,
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
if ($env:OS -notlike "*Windows*") {
    Write-Warning "This host does not look like Windows. Results are not the windows-latest liveness job."
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path (Get-Location) "windows-liveness-logs"
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"
$env:PYTHONUNBUFFERED = "1"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "python is not on PATH"
}
$version = & python -c "import sys; print(sys.version)"
Write-Host "python=$version"
Write-Host "os=$([System.Environment]::OSVersion.VersionString)"
Write-Host "log_dir=$LogDir"

function Invoke-Liveness {
    param(
        [string]$Root,
        [string]$Label,
        [int]$Rep
    )
    $resolved = (Resolve-Path $Root).Path
    $backend = Join-Path $resolved "backend"
    if (-not (Test-Path (Join-Path $backend "tests\test_issue572_event_loop_stalls.py"))) {
        throw "Missing liveness tests under $backend"
    }
    $sha = (& git -C $resolved rev-parse --short=12 HEAD).Trim()
    $log = Join-Path $LogDir "$Label-$sha-rep$Rep.log"
    Write-Host "BEGIN label=$Label sha=$sha rep=$Rep"
    & python -m pytest -q -rP -o faulthandler_timeout=300 `
        tests/test_issue572_event_loop_stalls.py `
        tests/test_issue517_windows_event_loop_liveness.py `
        tests/test_issue496_event_loop_responsiveness.py `
        --junitxml=(Join-Path $LogDir "$Label-$sha-rep$Rep.junit.xml") `
        *> $log
    $code = $LASTEXITCODE
    Add-Content -Path $log -Value "EXIT:$code"
    Write-Host "END label=$Label sha=$sha rep=$Rep exit=$code log=$log"
    return $code
}

$failures = @()
for ($rep = 1; $rep -le $Repeats; $rep++) {
    $firstCode = Invoke-Liveness -Root $FirstRoot -Label $FirstLabel -Rep $rep
    if ($firstCode -ne 0) { $failures += "$FirstLabel rep $rep exit $firstCode" }
    $secondCode = Invoke-Liveness -Root $SecondRoot -Label $SecondLabel -Rep $rep
    if ($secondCode -ne 0) { $failures += "$SecondLabel rep $rep exit $secondCode" }
}

Write-Host "MATRIX_DONE repeats=$Repeats failures=$($failures.Count)"
if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Host "FAIL $_" }
    exit 1
}
exit 0
