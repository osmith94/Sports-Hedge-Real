# Stop processes started by Start-SportsHedge-Demo.ps1.
# Never force-kill a reused PID. Identity JSON (pid + path + command tokens)
# must match the live process command/path before Stop-Process.
# Decision contract matches sports_hedge.application.demo_launcher_pid.

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function ConvertTo-DemoIdentity {
    param([string]$Raw)
    if ([string]::IsNullOrWhiteSpace($Raw)) {
        return $null
    }
    try {
        $parsed = $Raw | ConvertFrom-Json
        if ($null -eq $parsed.pid) {
            return $null
        }
        $tokens = @()
        if ($null -ne $parsed.command_tokens) {
            $tokens = @($parsed.command_tokens | ForEach-Object { [string]$_ })
        }
        return @{
            pid = [int]$parsed.pid
            path = [string]$parsed.path
            name = [string]$parsed.name
            command_tokens = $tokens
        }
    } catch {
        return $null
    }
}

function Get-DemoLiveProcess {
    param([int]$ProcId)
    $proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    $cim = $null
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    } catch {
        $cim = $null
    }
    $path = $null
    if ($null -ne $cim -and -not [string]::IsNullOrWhiteSpace($cim.ExecutablePath)) {
        $path = [string]$cim.ExecutablePath
    } elseif ($null -ne $proc -and -not [string]::IsNullOrWhiteSpace($proc.Path)) {
        $path = [string]$proc.Path
    }
    $command = $null
    if ($null -ne $cim) {
        $command = [string]$cim.CommandLine
    }
    $name = $null
    if ($null -ne $proc) {
        $name = [string]$proc.ProcessName
    }
    return @{
        present = ($null -ne $proc)
        path = $path
        name = $name
        command_line = $command
    }
}

function Test-DemoPidOwned {
    param(
        [hashtable]$Identity,
        [hashtable]$Live
    )
    if (-not $Live.present) {
        return "missing"
    }
    if ($null -eq $Identity -or $null -eq $Identity.pid) {
        return "stale"
    }
    if ($null -eq $Identity.command_tokens -or @($Identity.command_tokens).Count -eq 0) {
        return "stale"
    }
    $haystack = (@($Live.command_line, $Live.path, $Live.name) -join " ").ToLowerInvariant()
    foreach ($token in @($Identity.command_tokens)) {
        $needle = ([string]$token).ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($needle)) {
            continue
        }
        if ($haystack.IndexOf($needle) -lt 0) {
            return "stale"
        }
    }
    $expectedPath = ([string]$Identity.path).Replace("/", "\").Trim().ToLowerInvariant()
    $actualPath = ([string]$Live.path).Replace("/", "\").Trim().ToLowerInvariant()
    if ($expectedPath -and $actualPath -and $expectedPath -ne $actualPath) {
        return "stale"
    }
    return "stop"
}

function Stop-DemoPid {
    param(
        [string]$PidFile,
        [string]$Label
    )
    if (-not (Test-Path $PidFile)) {
        return
    }
    $raw = Get-Content $PidFile -Raw -ErrorAction SilentlyContinue
    $identity = ConvertTo-DemoIdentity -Raw $raw
    if ($null -eq $identity) {
        Write-Host "Ignoring untrusted/stale $Label PID file (no process identity). File removed without killing a process." -ForegroundColor Yellow
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    $live = Get-DemoLiveProcess -ProcId $identity.pid
    $action = Test-DemoPidOwned -Identity $identity -Live $live
    if ($action -eq "missing") {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    if ($action -ne "stop") {
        Write-Host "Stale $Label PID $($identity.pid) does not match the launcher-started process. PID file removed; unrelated process was not killed." -ForegroundColor Yellow
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    Write-Host "Stopping $Label PID $($identity.pid)"
    Stop-Process -Id $identity.pid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
    $still = Get-Process -Id $identity.pid -ErrorAction SilentlyContinue
    if ($null -ne $still) {
        Write-Host "Could not stop $Label PID $($identity.pid)" -ForegroundColor Yellow
        exit 1
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

$Root = Get-RepoRoot
$Logs = Join-Path $Root "logs"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-backend.pid") -Label "backend"
Stop-DemoPid -PidFile (Join-Path $Logs "demo-frontend.pid") -Label "frontend"

Write-Host "Sports Hedge paper demo stop completed. Logs retained under logs\."
exit 0
