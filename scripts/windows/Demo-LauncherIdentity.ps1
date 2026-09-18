# Shared PID / Git-HEAD identity contract for the Windows demo launchers.
# Decision contract matches sports_hedge.application.demo_launcher_pid.
# Dot-source only. Do not execute this file directly.

function ConvertTo-NormalizedRepoRoot {
    param([string]$PathValue)
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ""
    }
    return $PathValue.Replace("/", "\").Trim().TrimEnd("\").ToLowerInvariant()
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
        $gitHead = ""
        if ($null -ne $parsed.git_head) {
            $gitHead = [string]$parsed.git_head
        }
        $repoRoot = ""
        if ($null -ne $parsed.repo_root) {
            $repoRoot = [string]$parsed.repo_root
        }
        return @{
            pid = [int]$parsed.pid
            path = [string]$parsed.path
            name = [string]$parsed.name
            command_tokens = $tokens
            git_head = $gitHead
            repo_root = $repoRoot
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
    if ($null -eq $Live -or -not $Live.present) {
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

function Get-RepoGitIdentity {
    param([string]$RepoRoot)
    $sha = & git -C $RepoRoot rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sha)) {
        throw "git rev-parse HEAD failed in $RepoRoot"
    }
    $branch = & git -C $RepoRoot rev-parse --abbrev-ref HEAD
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
        throw "git rev-parse --abbrev-ref HEAD failed in $RepoRoot"
    }
    return @{
        sha = ([string]$sha).Trim()
        branch = ([string]$branch).Trim()
        repo_root = $RepoRoot
    }
}

function Get-DemoStartAction {
    param(
        [bool]$HealthOk,
        $Identity,
        $Live,
        [string]$CurrentGitHead,
        [string]$CurrentRepoRoot
    )
    if (-not $HealthOk) {
        return "start"
    }
    if ([string]::IsNullOrWhiteSpace($CurrentGitHead) -or [string]::IsNullOrWhiteSpace($CurrentRepoRoot)) {
        return "conflict"
    }
    if ($null -eq $Identity -or $null -eq $Live) {
        return "conflict"
    }
    $owned = Test-DemoPidOwned -Identity $Identity -Live $Live
    if ($owned -ne "stop") {
        return "conflict"
    }
    $recordedRoot = ConvertTo-NormalizedRepoRoot ([string]$Identity.repo_root)
    $currentRoot = ConvertTo-NormalizedRepoRoot $CurrentRepoRoot
    if ($recordedRoot -and $recordedRoot -ne $currentRoot) {
        return "conflict"
    }
    $recordedHead = ([string]$Identity.git_head).Trim().ToLowerInvariant()
    $currentHead = $CurrentGitHead.Trim().ToLowerInvariant()
    if ($recordedHead -and $recordedHead -eq $currentHead) {
        return "reuse"
    }
    return "restart"
}

function Write-DemoPidIdentity {
    param(
        [string]$PidFile,
        [System.Diagnostics.Process]$Process,
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
