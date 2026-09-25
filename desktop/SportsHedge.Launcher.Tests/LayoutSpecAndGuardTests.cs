using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Runtime;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.SingleInstance;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class SingleInstanceTests
{
    [Fact]
    public void Second_acquire_is_not_primary_until_the_first_releases()
    {
        var name = @"Local\SportsHedge.Tests." + Guid.NewGuid().ToString("N");
        using (var first = SingleInstanceGuard.Acquire(name))
        {
            Assert.True(first.IsPrimary);
            using var second = SingleInstanceGuard.Acquire(name);
            Assert.False(second.IsPrimary);
        }
        using var third = SingleInstanceGuard.Acquire(name);
        Assert.True(third.IsPrimary);
    }

    [Fact]
    public void Second_instance_signals_primary_and_never_starts_children()
    {
        var opened = new List<string>();
        var outcome = SecondaryInstance.Handle(new FakeActivation(true), opened.Add);
        Assert.Equal(SecondaryLaunchOutcome.SignalledPrimary, outcome);
        Assert.Empty(opened);
    }

    [Fact]
    public void Second_instance_falls_back_to_opening_the_existing_url()
    {
        var opened = new List<string>();
        var outcome = SecondaryInstance.Handle(new FakeActivation(false), opened.Add);
        Assert.Equal(SecondaryLaunchOutcome.OpenedBrowserDirectly, outcome);
        Assert.Equal(new[] { "http://127.0.0.1:3000/" }, opened);
    }

    [Fact]
    public void Tray_app_checks_single_instance_before_creating_a_controller()
    {
        var program = File.ReadAllText(Path.Combine(TestPaths.DesktopDir, "SportsHedge.Launcher", "Program.cs"));
        var guard = program.IndexOf("SingleInstanceGuard.Acquire", StringComparison.Ordinal);
        var secondary = program.IndexOf("SecondaryInstance.Handle", StringComparison.Ordinal);
        var factory = program.IndexOf("SessionFactory.Create", StringComparison.Ordinal);
        Assert.True(guard >= 0 && guard < secondary && secondary < factory);
    }

    private sealed class FakeActivation : IInstanceActivation
    {
        private readonly bool _answer;
        public FakeActivation(bool answer) => _answer = answer;
        public bool SignalPrimary() => _answer;
    }
}

public sealed class RepoLocatorTests
{
    [Fact]
    public void Finds_the_checkout_by_walking_up_from_dist_windows()
    {
        var dist = Path.Combine(TestPaths.RepoRoot, "dist", "windows");
        var location = RepoLocator.Locate(dist, null);
        Assert.True(location.Found, location.Detail);
        Assert.Equal(Path.GetFullPath(TestPaths.RepoRoot), location.RepoRoot);
    }

    [Fact]
    public void Honours_launcher_config_next_to_a_copied_exe()
    {
        var elsewhere = Path.Combine(Path.GetTempPath(), "sh-copied-exe", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(elsewhere);
        File.WriteAllText(Path.Combine(elsewhere, LauncherConstants.LauncherConfigFileName),
            System.Text.Json.JsonSerializer.Serialize(new { repo_root = TestPaths.RepoRoot }));
        var location = RepoLocator.Locate(elsewhere, null);
        Assert.True(location.Found, location.Detail);
    }

    [Fact]
    public void Rejects_an_override_that_is_not_a_sports_hedge_checkout()
    {
        var location = RepoLocator.Locate(Path.GetTempPath(), Path.GetTempPath());
        Assert.False(location.Found);
        Assert.Contains(LauncherConstants.RepoRootOverrideEnv, location.Detail);
    }
}

public sealed class ServiceSpecTests
{
    private readonly RuntimeLayout _layout = TestLayouts.Create();
    private readonly GitIdentity _git = new("abc1230000000000000000000000000000000000", "main");

    private ServiceSpecFactory Factory(RuntimeLayout layout) =>
        new(layout, SessionSecrets.Create(), new Dictionary<string, string>(), layout.IsWindows ? @"C:\Program Files\nodejs\npm.cmd" : "/usr/bin/npm");

    [Fact]
    public void Backend_runs_the_desktop_host_on_loopback_8000()
    {
        var spec = Factory(_layout).Backend(_git);
        Assert.Equal(_layout.PythonExecutable, spec.FileName);
        Assert.Equal(new[] { "-m", "sports_hedge.api.desktop_host", "--host", "127.0.0.1", "--port", "8000" }, spec.Arguments);
        Assert.Equal(_layout.BackendDir, spec.WorkingDirectory);
        Assert.EndsWith("desktop-backend.err.log", spec.StderrLog, StringComparison.Ordinal);
    }

    [Fact]
    public void Frontend_is_production_next_start_on_loopback_3000_never_dev()
    {
        foreach (var layout in new[] { _layout with { IsWindows = false }, _layout with { IsWindows = true } })
        {
            var spec = Factory(layout).Frontend(_git);
            var command = spec.DisplayCommand;
            Assert.Contains("run start -- -H 127.0.0.1 -p 3000", command);
            Assert.DoesNotContain(" dev", command);
            var build = Factory(layout).FrontendBuild(_git).DisplayCommand;
            Assert.Contains("run build", build);
            Assert.Contains("ci --include=dev --no-audit --no-fund", Factory(layout).FrontendInstall(_git).DisplayCommand);
        }
        var windows = Factory(_layout with { IsWindows = true }).Frontend(_git);
        Assert.Equal("/d /s /c \"\"C:\\Program Files\\nodejs\\npm.cmd\" run start -- -H 127.0.0.1 -p 3000\"", windows.RawArguments);
    }

    [Fact]
    public void Windows_layout_uses_the_backend_venv()
    {
        var windows = new RuntimeLayout(@"C:\Sports-Hedge", IsWindows: true);
        Assert.EndsWith(Path.Combine(".venv", "Scripts", "python.exe"), windows.PythonExecutable, StringComparison.Ordinal);
        Assert.EndsWith("sports-hedge-build.json", windows.FrontendBuildMarkerPath, StringComparison.Ordinal);
    }
}

public sealed class GitReaderTests
{
    [Fact]
    public void Reads_head_of_the_local_checkout_without_mutating_it()
    {
        var git = new GitCli();
        var identity = git.ReadIdentity(TestPaths.RepoRoot);
        Assert.Matches("^[0-9a-f]{40}$", identity.Sha);
        Assert.False(string.IsNullOrWhiteSpace(identity.Branch));
    }

    [Fact]
    public void Only_read_only_git_subcommands_are_allowed()
    {
        var verbs = GitCli.AllowedCommands.Select(c => c[0]).Distinct().OrderBy(v => v).ToArray();
        Assert.Equal(new[] { "rev-parse", "status" }, verbs);
    }
}

/// <summary>Static guards over the controller source.</summary>
public sealed class SourceGuardTests
{
    private static IEnumerable<(string Path, string Text)> ControllerSources() =>
        new[] { "SportsHedge.Launcher.Core", "SportsHedge.Launcher" }
            .SelectMany(project => Directory.EnumerateFiles(Path.Combine(TestPaths.DesktopDir, project), "*.cs", SearchOption.AllDirectories))
            .Where(path => !path.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}", StringComparison.Ordinal)
                           && !path.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            .Select(path => (path, File.ReadAllText(path)));

    [Fact]
    public void Controller_never_mutates_the_checkout_with_git()
    {
        foreach (var (path, text) in ControllerSources())
        {
            foreach (var verb in new[] { "\"pull\"", "\"fetch\"", "\"switch\"", "\"reset\"", "\"checkout\"", "\"clean\"", "\"merge\"" })
            {
                Assert.False(text.Contains(verb, StringComparison.Ordinal), $"{path} contains git verb {verb}");
            }
        }
    }

    [Fact]
    public void Controller_never_broad_kills_or_kills_by_name_or_port()
    {
        foreach (var (path, text) in ControllerSources())
        {
            Assert.DoesNotContain("GetProcessesByName", text);
            Assert.DoesNotContain("taskkill", text, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("Stop-Process", text);
            if (text.Contains(".Kill(", StringComparison.Ordinal))
            {
                var file = Path.GetFileName(path);
                Assert.True(file is "PosixProcessGroupLauncher.cs" or "GitReader.cs",
                    $"{path} kills a process; only owned-process groups (and our own timed-out git child) may");
            }
        }
    }

    [Fact]
    public void Controller_never_uses_next_dev_or_enables_execution()
    {
        foreach (var (path, text) in ControllerSources())
        {
            Assert.DoesNotContain("\"dev\"", text);
            Assert.DoesNotContain("run dev", text);
            Assert.DoesNotContain("SPORTS_HEDGE_EXECUTION_ENABLED\"] = \"true\"", text);
            Assert.DoesNotContain("place_order", text);
        }
    }
}
