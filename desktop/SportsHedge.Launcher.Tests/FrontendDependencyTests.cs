using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Build;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Runtime;
using SportsHedge.Launcher.Core.Security;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class FrontendDependencyDecisionTests
{
    private const string Lock = "aaaa";

    private static FrontendDependencyMarker Marker(string sha = Lock) => new() { PackageLockSha256 = sha, InstalledAt = "t", Installer = "test" };

    [Fact]
    public void Missing_node_modules_requires_npm_ci()
    {
        var decision = FrontendDependencyDecision.Evaluate(false, Marker(), Lock);
        Assert.True(decision.InstallRequired);
        Assert.Equal(DependencyDecisionReason.MissingNodeModules, decision.Reason);
    }

    [Fact]
    public void Matching_lockfile_fingerprint_skips_npm_ci()
    {
        var decision = FrontendDependencyDecision.Evaluate(true, Marker(), Lock);
        Assert.False(decision.InstallRequired);
        Assert.Equal(DependencyDecisionReason.Match, decision.Reason);
        Assert.False(FrontendDependencyDecision.Evaluate(true, Marker("AAAA"), Lock).InstallRequired);
    }

    [Fact]
    public void Changed_lockfile_fingerprint_requires_npm_ci()
    {
        var decision = FrontendDependencyDecision.Evaluate(true, Marker("bbbb"), Lock);
        Assert.True(decision.InstallRequired);
        Assert.Equal(DependencyDecisionReason.LockfileChanged, decision.Reason);
        Assert.Contains("node_modules installed from: bbbb", decision.Describe());
    }

    [Fact]
    public void Unrecorded_or_invalid_marker_requires_npm_ci()
    {
        Assert.Equal(DependencyDecisionReason.MissingMarker, FrontendDependencyDecision.Evaluate(true, null, Lock).Reason);
        Assert.Equal(DependencyDecisionReason.InvalidMarker, FrontendDependencyDecision.Evaluate(true, Marker() with { Schema = "x" }, Lock).Reason);
        Assert.Equal(DependencyDecisionReason.InvalidMarker, FrontendDependencyDecision.Evaluate(true, Marker(""), Lock).Reason);
    }

    [Fact]
    public void Fingerprint_is_sha256_of_the_lockfile_bytes()
    {
        var layout = TestLayouts.Create();
        File.WriteAllText(layout.PackageLockPath, "abc");
        Assert.Equal("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            FrontendDependencyMarkerStore.Fingerprint(layout.PackageLockPath));
        Assert.Null(FrontendDependencyMarkerStore.Fingerprint(Path.Combine(layout.FrontendDir, "missing.json")));
    }

    [Fact]
    public void Marker_lives_inside_node_modules()
    {
        var layout = TestLayouts.Create();
        Assert.StartsWith(layout.NodeModulesDir, layout.FrontendDependencyMarkerPath, StringComparison.Ordinal);
    }

    [Fact]
    public void Build_script_uses_the_same_dependency_marker_and_npm_ci_only()
    {
        var script = File.ReadAllText(Path.Combine(TestPaths.RepoRoot, "scripts", "windows", "Build-SportsHedge-App.ps1"));
        Assert.Contains(FrontendDependencyMarker.CurrentSchema, script);
        Assert.Contains("package_lock_sha256", script);
        Assert.Contains(".sports-hedge-deps.json", script);
        Assert.Contains("Get-FileHash", script);
        Assert.DoesNotContain("npm.cmd install", script);
    }
}

/// <summary>Real <see cref="NpmFrontendBuilder"/> against a scripted process launcher.</summary>
public sealed class NpmFrontendBuilderDependencyTests
{
    private static readonly GitIdentity Git = new("abc1230000000000000000000000000000000000", "main");
    private readonly RuntimeLayout _layout = TestLayouts.Create();
    private readonly ScriptedLauncher _launcher = new();
    private readonly RecordingLog _log = new();

    public NpmFrontendBuilderDependencyTests()
    {
        File.WriteAllText(_layout.PackageLockPath, "{\"lockfileVersion\":3}");
    }

    private NpmFrontendBuilder Builder() =>
        new(_layout, _launcher, new ServiceSpecFactory(_layout, SessionSecrets.Create(), new Dictionary<string, string>(), "npm"), _log);

    private string CurrentFingerprint => FrontendDependencyMarkerStore.Fingerprint(_layout.PackageLockPath)!;

    private void InstallRecorded(string fingerprint)
    {
        Directory.CreateDirectory(_layout.NodeModulesDir);
        FrontendDependencyMarkerStore.Write(_layout.FrontendDependencyMarkerPath, new FrontendDependencyMarker
        {
            PackageLockSha256 = fingerprint,
            InstalledAt = "2026-09-25T00:00:00Z",
            Installer = "test",
        });
    }

    [Fact]
    public async Task Missing_node_modules_runs_npm_ci_and_records_the_lockfile()
    {
        _launcher.OnStart = spec =>
        {
            Directory.CreateDirectory(_layout.NodeModulesDir);
            return 0;
        };

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.True(result.Success, result.Detail);
        var spec = Assert.Single(_launcher.Started);
        Assert.Equal("frontend-install", spec.Label);
        Assert.Contains("ci --include=dev --no-audit --no-fund", spec.DisplayCommand);
        Assert.False(spec.Environment.ContainsKey("NODE_ENV"), "npm ci must install devDependencies");
        Assert.Equal(CurrentFingerprint, FrontendDependencyMarkerStore.Read(_layout.FrontendDependencyMarkerPath)!.PackageLockSha256);
        Assert.True(_log.Contains("reason=MissingNodeModules"));
    }

    [Fact]
    public async Task Matching_fingerprint_skips_npm_ci()
    {
        InstallRecorded(CurrentFingerprint);

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.True(result.Success);
        Assert.Empty(_launcher.Started);
        Assert.True(_log.Contains("install=False reason=Match"));
    }

    [Fact]
    public async Task Changed_package_lock_runs_npm_ci_and_records_the_new_fingerprint()
    {
        InstallRecorded(CurrentFingerprint);
        var before = File.ReadAllText(_layout.PackageLockPath);
        File.WriteAllText(_layout.PackageLockPath, "{\"lockfileVersion\":3,\"packages\":{\"next\":\"15.5.1\"}}");
        var changedLock = File.ReadAllText(_layout.PackageLockPath);

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.True(result.Success, result.Detail);
        Assert.Single(_launcher.Started);
        Assert.True(_log.Contains("reason=LockfileChanged"));
        Assert.Equal(CurrentFingerprint, FrontendDependencyMarkerStore.Read(_layout.FrontendDependencyMarkerPath)!.PackageLockSha256);
        Assert.NotEqual(before, changedLock);
        Assert.Equal(changedLock, File.ReadAllText(_layout.PackageLockPath));
    }

    [Fact]
    public async Task Existing_node_modules_without_a_marker_runs_npm_ci_once()
    {
        Directory.CreateDirectory(_layout.NodeModulesDir);

        var first = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);
        var second = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.True(first.Success && second.Success);
        Assert.Single(_launcher.Started);
        Assert.True(_log.Contains("reason=MissingMarker"));
    }

    [Fact]
    public async Task Npm_ci_failure_fails_and_leaves_no_dependency_marker()
    {
        InstallRecorded("0000000000000000000000000000000000000000000000000000000000000000");
        var markerSeenDuringInstall = true;
        _launcher.OnStart = _ =>
        {
            markerSeenDuringInstall = File.Exists(_layout.FrontendDependencyMarkerPath);
            File.WriteAllText(_layout.FrontendBuildErrLog, "npm ERR! code EINTEGRITY");
            return 1;
        };

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("npm ci failed with exit code 1", result.Detail);
        Assert.Contains("EINTEGRITY", result.Detail);
        Assert.False(markerSeenDuringInstall, "stale marker must be removed before npm ci runs");
        Assert.False(File.Exists(_layout.FrontendDependencyMarkerPath));
    }

    [Fact]
    public async Task Marker_is_written_only_after_npm_ci_succeeds()
    {
        var markerExistedWhileRunning = true;
        _launcher.OnStart = _ =>
        {
            markerExistedWhileRunning = File.Exists(_layout.FrontendDependencyMarkerPath);
            Directory.CreateDirectory(_layout.NodeModulesDir);
            return 0;
        };

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.True(result.Success);
        Assert.False(markerExistedWhileRunning);
        Assert.True(File.Exists(_layout.FrontendDependencyMarkerPath));
    }

    [Fact]
    public async Task Npm_ci_that_rewrites_the_lockfile_or_manifest_fails_and_records_nothing()
    {
        File.WriteAllText(_layout.PackageJsonPath, "{\"name\":\"x\"}");
        _launcher.OnStart = _ =>
        {
            Directory.CreateDirectory(_layout.NodeModulesDir);
            File.WriteAllText(_layout.PackageJsonPath, "{\"name\":\"x\",\"devDependencies\":{\"typescript\":\"5.8.2\"}}");
            return 0;
        };

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("modified frontend", result.Detail);
        Assert.False(File.Exists(_layout.FrontendDependencyMarkerPath));
        Assert.True(_log.Contains("checkout_mutated"));
    }

    [Fact]
    public async Task Build_that_rewrites_package_json_fails_and_writes_no_build_marker()
    {
        File.WriteAllText(_layout.PackageJsonPath, "{\"name\":\"x\"}");
        _launcher.OnStart = _ =>
        {
            File.WriteAllText(_layout.NextBuildIdPath, "b1");
            File.WriteAllText(_layout.PackageJsonPath, "{\"name\":\"x\",\"devDependencies\":{\"@types/node\":\"20.17.6\"}}");
            return 0;
        };

        var result = await Builder().BuildAsync(Git, frontendDirty: false, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("modified frontend", result.Detail);
        Assert.False(File.Exists(_layout.FrontendBuildMarkerPath));
    }

    [Fact]
    public async Task Missing_lockfile_fails_without_running_npm()
    {
        File.Delete(_layout.PackageLockPath);

        var result = await Builder().EnsureDependenciesAsync(Git, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("package-lock.json", result.Detail);
        Assert.Empty(_launcher.Started);
    }

    private sealed class ScriptedLauncher : IProcessGroupLauncher
    {
        private readonly Journal _journal = new();
        public Func<ProcessSpec, int> OnStart { get; set; } = _ => 0;
        public List<ProcessSpec> Started { get; } = new();
        public bool ProvidesKillOnClose => true;

        public IOwnedProcessGroup Start(ProcessSpec spec)
        {
            Started.Add(spec);
            var code = OnStart(spec);
            var group = new FakeGroup(spec.Label, 5000 + Started.Count, _journal);
            group.ExitNaturally(code);
            return group;
        }
    }
}
