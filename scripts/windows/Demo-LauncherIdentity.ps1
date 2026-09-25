# Shared PID / Git-HEAD identity contract for the Windows demo launchers.
# Decision contract matches sports_hedge.application.demo_launcher_pid.
# Owned stop also terminates verified ParentProcessId descendants of that PID.
# Never broad-kill Node. Unrelated port occupants are refused, not killed.
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
        [bool]$PortListening,
        $Identity,
        $Live,
        [string]$CurrentGitHead,
        [string]$CurrentRepoRoot
    )
    if (-not $PortListening) {
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

function Get-DemoCimProcessSnapshot {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    } catch {
        return @()
    }
}

function Get-DemoOwnedTreePids {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RootPid,
        $Processes = $null
    )
    if ($null -eq $Processes) {
        $Processes = Get-DemoCimProcessSnapshot
    }
    $children = @{}
    foreach ($proc in @($Processes)) {
        $pidValue = $null
        $ppid = $null
        try {
            if ($null -ne $proc.ProcessId) {
                $pidValue = [int]$proc.ProcessId
                $ppid = [int]$proc.ParentProcessId
            } elseif ($null -ne $proc.pid) {
                $pidValue = [int]$proc.pid
                $ppid = [int]$proc.parent_pid
            }
        } catch {
            continue
        }
        if ($null -eq $pidValue -or $pidValue -eq $ppid) {
            continue
        }
        if (-not $children.ContainsKey($ppid)) {
            $children[$ppid] = New-Object System.Collections.Generic.List[int]
        }
        [void]$children[$ppid].Add($pidValue)
    }
    $ordered = New-Object System.Collections.Generic.List[int]
    $visited = New-Object "System.Collections.Generic.HashSet[int]"
    $stack = New-Object System.Collections.Generic.Stack[object]
    $stack.Push(@{ Pid = $RootPid; Expanded = $false })
    while ($stack.Count -gt 0) {
        $frame = $stack.Pop()
        $pidValue = [int]$frame.Pid
        if ($frame.Expanded) {
            [void]$ordered.Add($pidValue)
            continue
        }
        if ($visited.Contains($pidValue)) {
            continue
        }
        [void]$visited.Add($pidValue)
        $stack.Push(@{ Pid = $pidValue; Expanded = $true })
        $childList = @()
        if ($children.ContainsKey($pidValue)) {
            $childList = @($children[$pidValue])
        }
        for ($i = $childList.Count - 1; $i -ge 0; $i--) {
            $child = [int]$childList[$i]
            if (-not $visited.Contains($child)) {
                $stack.Push(@{ Pid = $child; Expanded = $false })
            }
        }
    }
    if ($ordered.Count -eq 0) {
        [void]$ordered.Add($RootPid)
    }
    return @($ordered)
}

function Test-DemoDescendantOwned {
    param(
        [hashtable]$Identity,
        [int[]]$TreePids,
        [string]$Label,
        [int]$DescendantPid,
        [hashtable]$Live
    )
    if ($null -eq $TreePids -or (@($TreePids) -notcontains $DescendantPid)) {
        return "refuse"
    }
    if ($null -eq $Live -or -not $Live.present) {
        return "missing"
    }
    $name = ([string]$Live.name).Trim().ToLowerInvariant()
    if ($name.EndsWith(".exe")) {
        $name = $name.Substring(0, $name.Length - 4)
    }
    $wrappers = @("cmd", "npm", "conhost", "powershell", "pwsh")
    if ($wrappers -contains $name) {
        return "stop"
    }
    $haystack = (@($Live.command_line, $Live.path, $Live.name) -join " ").ToLowerInvariant()
    $repo = ConvertTo-NormalizedRepoRoot ([string]$Identity.repo_root)
    $kind = ([string]$Label).Trim().ToLowerInvariant()
    $commandAvailable = -not [string]::IsNullOrWhiteSpace([string]$Live.command_line)
    if ($kind -eq "frontend") {
        $looksNode = ($name -eq "node") -or ($name -eq "nodejs") -or ($haystack.IndexOf("node") -ge 0) -or ($haystack.IndexOf("next") -ge 0)
        if (-not $looksNode) {
            return "refuse"
        }
        if (-not $commandAvailable) {
            return "stop"
        }
        if ($repo -and $haystack.IndexOf($repo) -ge 0) {
            return "stop"
        }
        foreach ($token in @("next", "frontend", "3000", "npm")) {
            if ($haystack.IndexOf($token) -ge 0) {
                return "stop"
            }
        }
        return "refuse"
    }
    if ($kind -eq "backend") {
        $looksPython = ($name -eq "python") -or ($name -eq "pythonw") -or ($name -eq "py") -or ($haystack.IndexOf("python") -ge 0) -or ($haystack.IndexOf("uvicorn") -ge 0)
        if (-not $looksPython) {
            return "refuse"
        }
        if (-not $commandAvailable) {
            return "stop"
        }
        if ($repo -and $haystack.IndexOf($repo) -ge 0) {
            return "stop"
        }
        foreach ($token in @("uvicorn", "sports_hedge.api.main:app")) {
            if ($haystack.IndexOf($token) -ge 0) {
                return "stop"
            }
        }
        return "refuse"
    }
    return "refuse"
}

function Test-DemoPortListening {
    param([int]$Port)
    try {
        $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($conns.Count -gt 0) {
            return $true
        }
    } catch {
        # Fall through to netstat; some hosts restrict Get-NetTCPConnection.
    }
    try {
        $escaped = [regex]::Escape([string]$Port)
        # netstat -ano columns: Proto  Local  Foreign  State  PID
        $pattern = "[:.]$escaped\s+\S+\s+LISTENING"
        $lines = @(netstat -ano | Select-String -Pattern $pattern)
        return ($lines.Count -gt 0)
    } catch {
        return $false
    }
}

function Wait-DemoPortGone {
    param(
        [int]$Port,
        [int]$Seconds = 10,
        [string]$Label
    )
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (-not (Test-DemoPortListening -Port $Port)) {
            return
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "$Label port $Port is still listening after stopping the launcher-owned process tree. Refusing to kill an unexpected occupant of the port." -ForegroundColor Yellow
    exit 1
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
    $snapshot = Get-DemoCimProcessSnapshot
    $treePids = @(Get-DemoOwnedTreePids -RootPid $identity.pid -Processes $snapshot)
    $stopPids = New-Object System.Collections.Generic.List[int]
    foreach ($procId in $treePids) {
        if ([int]$procId -eq [int]$identity.pid) {
            [void]$stopPids.Add([int]$procId)
            continue
        }
        $childLive = Get-DemoLiveProcess -ProcId ([int]$procId)
        $childAction = Test-DemoDescendantOwned -Identity $identity -TreePids $treePids -Label $Label -DescendantPid ([int]$procId) -Live $childLive
        if ($childAction -eq "stop") {
            [void]$stopPids.Add([int]$procId)
        } elseif ($childAction -eq "refuse") {
            Write-Host "Refusing to stop $Label descendant PID $procId; not a verified Sports Hedge descendant. Unrelated process was not killed." -ForegroundColor Yellow
        }
    }
    foreach ($procId in @($stopPids)) {
        Write-Host "Stopping $Label PID $procId"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(5)
    foreach ($procId in @($stopPids)) {
        while ((Get-Date) -lt $deadline) {
            $still = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($null -eq $still) {
                break
            }
            Start-Sleep -Milliseconds 200
        }
        $still = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($null -ne $still) {
            Write-Host "Could not stop $Label PID $procId" -ForegroundColor Yellow
            exit 1
        }
    }
    if ($Label -eq "frontend") {
        Wait-DemoPortGone -Port 3000 -Label $Label
    } elseif ($Label -eq "backend") {
        Wait-DemoPortGone -Port 8000 -Label $Label
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}
