# Sports Hedge - reproducible build of the Windows desktop controller (SportsHedge.exe).
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\Build-SportsHedge-App.ps1
#
# 1. Validates prerequisites (git, Node/npm, .NET 8 SDK) and locates the repo root.
# 2. Reports the Git SHA / branch being built.
# 3. Frontend: npm ci when node_modules is missing or its recorded
#    package-lock.json SHA-256 differs, typecheck, tests, production
#    `npm run build`, then writes frontend\.next\sports-hedge-build.json
#    (schema sports-hedge-frontend-build/v1: git_sha, git_branch,
#    build_timestamp, next_build_id, frontend_dirty, builder).
# 4. Builds and tests desktop\SportsHedge.Desktop.sln, then publishes a
#    self-contained single-file win-x64 SportsHedge.exe to dist\windows\.
#
# SportsHedge.exe runs whatever local checkout it belongs to. It never
# fetches/pulls/switches/resets Git and rebuilds the frontend itself when the
# recorded build SHA differs from HEAD, so this script only needs re-running
# when desktop\ controller code changes. Python and Node are NOT bundled.
# Does not enable live execution, wallet signing, or trading credentials.

[CmdletBinding()]
param(
    [switch]$SkipFrontendChecks,
    [switch]$SkipDotnetTests,
    [switch]$CreateDesktopShortcut,
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Demo-LauncherIdentity.ps1")

function Fail {
    param([string]$Message)
    Write-Host "BUILD FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Step {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Fail "$Label (exit code $LASTEXITCODE)"
    }
}

function Get-ManifestFingerprint {
    return (@("package.json", "package-lock.json") | ForEach-Object {
        (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $FrontendDir $_)).Hash
    }) -join ","
}

function Assert-ManifestUnchanged {
    param([string]$Label)
    if ((Get-ManifestFingerprint) -ne $ManifestFingerprint) {
        Fail "$Label modified frontend\package.json or frontend\package-lock.json. Inspect with 'git diff frontend'; nothing was reverted."
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
foreach ($required in @(".git", "backend\pyproject.toml", "backend\src\sports_hedge\api\desktop_host.py", "frontend\package.json", "desktop\SportsHedge.Desktop.sln")) {
    if (-not (Test-Path (Join-Path $Root $required))) {
        Fail "Could not resolve the Sports Hedge repository root from $PSScriptRoot (missing $required)."
    }
}
$FrontendDir = Join-Path $Root "frontend"
$DesktopDir = Join-Path $Root "desktop"
$Solution = Join-Path $DesktopDir "SportsHedge.Desktop.sln"
$AppProject = Join-Path $DesktopDir "SportsHedge.Launcher\SportsHedge.Launcher.csproj"
$DistDir = Join-Path $Root "dist\windows"
$ExePath = Join-Path $DistDir "SportsHedge.exe"
$NextDir = Join-Path $FrontendDir ".next"
$MarkerPath = Join-Path $NextDir "sports-hedge-build.json"
$BuildIdPath = Join-Path $NextDir "BUILD_ID"
$PackageLockPath = Join-Path $FrontendDir "package-lock.json"
$NodeModulesDir = Join-Path $FrontendDir "node_modules"
$DepsMarkerPath = Join-Path $NodeModulesDir ".sports-hedge-deps.json"
if (-not (Test-Path $PackageLockPath)) {
    Fail "frontend\package-lock.json is missing; npm ci requires the committed lockfile."
}
$ManifestFingerprint = Get-ManifestFingerprint

foreach ($tool in @("git", "node", "npm.cmd", "dotnet")) {
    if ($null -eq (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool was not found on PATH. Required: Git, Node.js 22 (npm), .NET 8 SDK."
    }
}
$dotnetVersion = (& dotnet --version).Trim()
if ([int]($dotnetVersion.Split(".")[0]) -lt 8) {
    Fail ".NET SDK 8 or later is required (found $dotnetVersion)."
}
if (-not (Test-Path (Join-Path $Root "backend\.venv\Scripts\python.exe"))) {
    Write-Host "WARNING: backend\.venv is missing. SportsHedge.exe needs it at runtime:" -ForegroundColor Yellow
    Write-Host "  cd backend; python -m venv .venv; .venv\Scripts\python -m pip install -e `".[dev]`"" -ForegroundColor Yellow
}

try {
    $Git = Get-RepoGitIdentity -RepoRoot $Root
} catch {
    Fail "Could not read Git HEAD from $Root. $_"
}
Write-Host "Repository: $Root"
Write-Host "Current branch: $($Git.branch)"
Write-Host "Current SHA: $($Git.sha)"
Write-Host ".NET SDK: $dotnetVersion"

if (Test-Path $ExePath) {
    try {
        $stream = [System.IO.File]::Open($ExePath, "Open", "ReadWrite", "None")
        $stream.Close()
    } catch {
        Fail "$ExePath is in use. Exit Sports Hedge (tray -> Exit Sports Hedge) before rebuilding the controller."
    }
}

# ---- Frontend (production Next.js) ----
Push-Location $FrontendDir
try {
    # Same contract as SportsHedge.exe: npm ci (never npm install) when
    # node_modules is missing or was not installed from this exact lockfile.
    $lockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PackageLockPath).Hash.ToLowerInvariant()
    $recordedHash = $null
    if (Test-Path $DepsMarkerPath) {
        try {
            $depsMarker = Get-Content -Raw $DepsMarkerPath | ConvertFrom-Json
            if ($depsMarker.schema -eq "sports-hedge-frontend-deps/v1") {
                $recordedHash = [string]$depsMarker.package_lock_sha256
            }
        } catch {
            $recordedHash = $null
        }
    }
    if (-not (Test-Path $NodeModulesDir) -or $recordedHash -ne $lockHash) {
        Write-Host "Frontend dependencies: package-lock.json sha256 $lockHash; node_modules installed from $(if ($recordedHash) { $recordedHash } else { '(unknown)' })"
        if (Test-Path $DepsMarkerPath) {
            Remove-Item -LiteralPath $DepsMarkerPath -Force
        }
        # NODE_ENV=production would make npm ci omit devDependencies, and
        # `next build` would then npm-install them itself, rewriting package.json.
        Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue
        Remove-Item Env:NPM_CONFIG_PRODUCTION -ErrorAction SilentlyContinue
        Remove-Item Env:NPM_CONFIG_OMIT -ErrorAction SilentlyContinue
        Invoke-Step "npm ci (frontend dependencies)" { & npm.cmd ci --include=dev --no-audit --no-fund }
        Assert-ManifestUnchanged "npm ci"
        $depsRecord = [ordered]@{
            schema = "sports-hedge-frontend-deps/v1"
            package_lock_sha256 = $lockHash
            installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            installer = "Build-SportsHedge-App.ps1"
        }
        ($depsRecord | ConvertTo-Json) | Set-Content -Path $DepsMarkerPath -Encoding utf8
    } else {
        Write-Host "Frontend dependencies match package-lock.json ($lockHash); skipping npm ci."
    }
    if (-not $SkipFrontendChecks) {
        Invoke-Step "Frontend typecheck" { & npm.cmd run typecheck }
        Invoke-Step "Frontend tests" { & npm.cmd test }
    }

    # `next build` inlines NEXT_PUBLIC_* into browser bundles. No desktop
    # identity or secret may be present in this environment.
    Get-ChildItem Env: | Where-Object { $_.Name -like "SPORTS_HEDGE_CONTROLLER_*" -or $_.Name -like "SPORTS_HEDGE_DESKTOP_*" } |
        ForEach-Object { Remove-Item "Env:$($_.Name)" }
    $env:NEXT_PUBLIC_SPORTS_HEDGE_API_URL = "http://127.0.0.1:8000"
    $env:NEXT_TELEMETRY_DISABLED = "1"
    $env:SPORTS_HEDGE_MODE = "paper"
    $env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"

    if (Test-Path $MarkerPath) {
        Remove-Item -LiteralPath $MarkerPath -Force
    }
    Invoke-Step "Production frontend build (npm run build)" { & npm.cmd run build }
    Assert-ManifestUnchanged "npm run build"
} finally {
    Pop-Location
}

if (-not (Test-Path $BuildIdPath)) {
    Fail "npm run build succeeded but frontend\.next\BUILD_ID is missing."
}
$buildId = (Get-Content -Raw $BuildIdPath).Trim()
$frontendStatus = & git -C $Root status --porcelain --untracked-files=normal -- frontend
$frontendDirty = -not [string]::IsNullOrWhiteSpace(($frontendStatus | Out-String))
$marker = [ordered]@{
    schema = "sports-hedge-frontend-build/v1"
    git_sha = $Git.sha
    git_branch = $Git.branch
    build_timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    next_build_id = $buildId
    frontend_dirty = $frontendDirty
    builder = "Build-SportsHedge-App.ps1"
}
($marker | ConvertTo-Json) | Set-Content -Path $MarkerPath -Encoding utf8
Write-Host "Frontend build marker: $MarkerPath (git_sha=$($Git.sha), next_build_id=$buildId, frontend_dirty=$frontendDirty)"

# ---- Desktop controller (.NET 8) ----
Invoke-Step "dotnet restore" { & dotnet restore $Solution }
Invoke-Step "dotnet build ($Configuration)" { & dotnet build $Solution -c $Configuration --no-restore }
if (-not $SkipDotnetTests) {
    Invoke-Step "dotnet test (launcher unit + Windows Job Object tests)" { & dotnet test $Solution -c $Configuration --no-build }
}
Invoke-Step "dotnet publish win-x64 self-contained single-file" {
    & dotnet publish $AppProject -c $Configuration -r win-x64 --self-contained true -p:PublishSingleFile=true -o $DistDir
}
if (-not (Test-Path $ExePath)) {
    Fail "Publish finished but $ExePath was not produced."
}

$launcherConfig = [ordered]@{
    repo_root = $Root
    controller_built_from_sha = $Git.sha
    controller_built_from_branch = $Git.branch
    controller_built_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
($launcherConfig | ConvertTo-Json) | Set-Content -Path (Join-Path $DistDir "SportsHedge.launcher.json") -Encoding utf8

if ($CreateDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Sports Hedge.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $ExePath
    $shortcut.WorkingDirectory = $DistDir
    $shortcut.Description = "Sports Hedge (PAPER MODE)"
    $shortcut.Save()
    Write-Host "Desktop shortcut: $shortcutPath"
}

$exe = Get-Item $ExePath
Write-Host ""
Write-Host "Sports Hedge desktop controller built." -ForegroundColor Green
Write-Host "Executable: $($exe.FullName) ($([math]::Round($exe.Length / 1MB, 1)) MB)"
Write-Host "Built from: $($Git.sha) ($($Git.branch))"
Write-Host "Launch: double-click dist\windows\SportsHedge.exe (PAPER MODE, execution disabled)."
Write-Host "Rebuild SportsHedge.exe only when desktop\ controller code changes; backend/frontend changes are picked up from the local checkout."
exit 0
