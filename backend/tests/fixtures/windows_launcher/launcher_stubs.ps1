# Deterministic OS/process/HTTP stubs for running the real Windows demo
# launcher under pwsh in tests. Dot-sourced at the end of the copied
# Demo-LauncherIdentity.ps1 so these definitions shadow the real helpers and
# cmdlets. Scenario JSON: $env:SH_LAUNCHER_SCENARIO. Event log: $env:SH_LAUNCHER_EVENTS.

$script:StubScenario = Get-Content $env:SH_LAUNCHER_SCENARIO -Raw | ConvertFrom-Json
$script:StubStopped = New-Object "System.Collections.Generic.HashSet[int]"
$script:StubNextPid = 9000

function Write-StubEvent {
    param([string]$Kind, [string]$Value)
    Add-Content -Path $env:SH_LAUNCHER_EVENTS -Value "$Kind`t$Value"
}

function Get-StubProcess {
    param([int]$ProcId)
    if ($script:StubStopped.Contains($ProcId)) {
        return $null
    }
    foreach ($proc in @($script:StubScenario.processes)) {
        if ([int]$proc.pid -eq $ProcId) {
            if ($null -ne $proc.exits_after_checks -and $script:StubTotalChecks -ge [int]$proc.exits_after_checks) {
                return $null
            }
            return $proc
        }
    }
    return $null
}

function Get-RepoGitIdentity {
    param([string]$RepoRoot)
    return @{ sha = [string]$script:StubScenario.git_sha; branch = "stub-branch"; repo_root = $RepoRoot }
}

# Port spec: listener_pids (PIDs owning the listener), listen_after_checks
# (listener appears only from the Nth check of that port), takeover_pids
# (listener once every listener_pids process is gone).
# Process spec: exits_after_checks (process gone from the Nth port check).
$script:StubPortChecks = @{}
$script:StubTotalChecks = 0

function Get-DemoPortListenerPids {
    param([int]$Port)
    Write-StubEvent "PORTCHECK" ([string]$Port)
    $key = [string]$Port
    $script:StubPortChecks[$key] = 1 + [int]$script:StubPortChecks[$key]
    $script:StubTotalChecks += 1
    $spec = $script:StubScenario.ports.$key
    if ($null -eq $spec) {
        return @()
    }
    if ($null -ne $spec.listen_after_checks -and $script:StubPortChecks[$key] -lt [int]$spec.listen_after_checks) {
        return @()
    }
    $alive = @(@($spec.listener_pids) | Where-Object { $null -ne $_ -and $null -ne (Get-StubProcess -ProcId ([int]$_)) } | ForEach-Object { [int]$_ })
    if ($alive.Count -gt 0) {
        return $alive
    }
    return @(@($spec.takeover_pids) | Where-Object { $null -ne $_ } | ForEach-Object { [int]$_ })
}

function Get-DemoLiveProcess {
    param([int]$ProcId)
    $proc = Get-StubProcess -ProcId $ProcId
    if ($null -eq $proc) {
        return @{ present = $false; path = $null; name = $null; command_line = $null }
    }
    return @{
        present = $true
        path = [string]$proc.path
        name = [string]$proc.name
        command_line = [string]$proc.command_line
    }
}

function Get-DemoCimProcessSnapshot {
    $rows = @()
    foreach ($proc in @($script:StubScenario.processes)) {
        if ($null -eq (Get-StubProcess -ProcId ([int]$proc.pid))) {
            continue
        }
        $rows += [pscustomobject]@{ ProcessId = [int]$proc.pid; ParentProcessId = [int]$proc.parent_pid }
    }
    return $rows
}

function Get-Process {
    param([int]$Id, $ErrorAction)
    $proc = Get-StubProcess -ProcId $Id
    if ($null -eq $proc) {
        return $null
    }
    return [pscustomobject]@{ Id = [int]$proc.pid; ProcessName = [string]$proc.name }
}

function Stop-Process {
    param([int]$Id, [switch]$Force, $ErrorAction)
    Write-StubEvent "STOP" ([string]$Id)
    [void]$script:StubStopped.Add($Id)
}

function Start-Sleep {
    param([int]$Seconds, [int]$Milliseconds)
}

function Add-Type {
    throw "no GUI in tests"
}

function Get-Command {
    param([string]$Name, $ErrorAction)
    return [pscustomobject]@{ Name = $Name; Source = "C:\nodejs\$Name" }
}

function Test-Path {
    param([string]$Path)
    if ($Path -like "*python.exe") {
        return $true
    }
    return Microsoft.PowerShell.Management\Test-Path -LiteralPath $Path
}

function Start-Process {
    param(
        [string]$FilePath,
        [object[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$WindowStyle,
        [string]$RedirectStandardOutput,
        [string]$RedirectStandardError,
        [switch]$PassThru
    )
    if ($null -eq $ArgumentList -or @($ArgumentList).Count -eq 0) {
        Write-StubEvent "BROWSER" $FilePath
        return
    }
    $script:StubNextPid += 1
    $envSummary = "SPORTS_HEDGE_MODE=$env:SPORTS_HEDGE_MODE SPORTS_HEDGE_EXECUTION_ENABLED=$env:SPORTS_HEDGE_EXECUTION_ENABLED"
    Write-StubEvent "START" ("$FilePath " + (@($ArgumentList) -join " ") + " | $envSummary")
    return [pscustomobject]@{ Id = $script:StubNextPid; Path = $FilePath; ProcessName = "started" }
}

function Write-DemoPidIdentity {
    param(
        [string]$PidFile,
        $Process,
        [string[]]$CommandTokens,
        [string]$GitHead,
        [string]$RepoRoot
    )
    $payload = @{
        pid = $Process.Id
        path = $Process.Path
        name = $Process.ProcessName
        command_tokens = @($CommandTokens)
        git_head = $GitHead
        repo_root = $RepoRoot
    }
    ($payload | ConvertTo-Json -Compress) | Set-Content -Path $PidFile -Encoding utf8
}

function Invoke-WebRequest {
    param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec)
    Write-StubEvent "HTTP" $Uri
    if (@($script:StubScenario.http_ok) -contains $Uri) {
        return [pscustomobject]@{ StatusCode = 200 }
    }
    throw "The operation has timed out."
}
