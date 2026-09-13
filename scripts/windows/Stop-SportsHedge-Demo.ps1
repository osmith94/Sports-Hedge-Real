# Stop processes started by Start-SportsHedge-Demo.ps1.
# Does not kill unrelated listeners on 8000/3000 unless the PID file matches.

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Stop-DemoPid {
    param(
        [string]$PidFile,
        [string]$Label
    )
    if (-not (Test-Path $PidFile)) {
        return
    }
    $raw = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    $procId = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$procId)) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -ne $proc) {
        Write-Host "Stopping $Label PID $procId"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 400
        $still = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($null -ne $still) {
            Write-Host "Could not stop $Label PID $procId" -ForegroundColor Yellow
            exit 1
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-backend.pid") -Label "backend"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-frontend.pid") -Label "frontend"

Write-Host "Sports Hedge paper demo stop completed. Logs retained under logs\."
exit 0
