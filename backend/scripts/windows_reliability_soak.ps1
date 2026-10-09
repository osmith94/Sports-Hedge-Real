# 30-minute execution-disabled synthetic soak.
#
# Run on Windows from the PR checkout after:
#
#   python -m pip install -e ".\backend[dev]"
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\windows_reliability_soak.ps1
#
# The Python process forces PAPER_LIVE_REFRESH_ENABLED for the in-process
# scheduler only. SPORTS_HEDGE_EXECUTION_ENABLED is forced false. Providers are
# synthetic. A run on any other OS is not the required Windows evidence.
#
# Extra arguments are forwarded, for example: --seconds 60

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Forward = @()
)

$ErrorActionPreference = "Stop"
if ($env:OS -notlike "*Windows*") {
    Write-Warning "This host does not look like Windows. Do not file the JSON as the Windows soak."
}

$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"
$env:PYTHONUNBUFFERED = "1"
Remove-Item Env:SPORTS_HEDGE_KALSHI_OPERATOR -ErrorAction SilentlyContinue

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
Set-Location $repo

Write-Host "python=$((& python -c 'import sys; print(sys.version)'))"
Write-Host "os=$([System.Environment]::OSVersion.VersionString)"
Write-Host "repo=$repo"
Write-Host "execution=$($env:SPORTS_HEDGE_EXECUTION_ENABLED)"

$soak = Join-Path $scriptDir "windows_reliability_soak.py"
if ($Forward.Count -gt 0) {
    & python $soak @Forward
} else {
    & python $soak --seconds 1800 --fixtures 941 --watched 100 --stress-watched 500
}
exit $LASTEXITCODE
