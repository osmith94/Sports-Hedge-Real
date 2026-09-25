using System.Diagnostics;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Processes.Windows;
using SportsHedge.Launcher.Core.ProcessEnvironment;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class JobOwnedProcessGroupTests
{
    [Fact]
    public void Group_reports_job_members_and_terminates_only_through_its_job()
    {
        var job = new FakeJob(10, 11, 12);
        var root = new FakeRoot(10);
        using var group = new JobOwnedProcessGroup("backend", job, root);

        Assert.Equal(new[] { 10, 11, 12 }, group.ActiveProcessIds());
        Assert.Equal(10, group.RootProcessId);

        group.TerminateAll();

        Assert.Equal(1, job.TerminateCalls);
        Assert.Empty(group.ActiveProcessIds());
    }

    [Fact]
    public async Task Wait_for_all_exited_tracks_descendants_not_just_the_root()
    {
        var job = new FakeJob(10, 11);
        var root = new FakeRoot(10);
        using var group = new JobOwnedProcessGroup("frontend", job, root);
        root.Exit(0);

        Assert.True(group.RootHasExited);
        Assert.False(await group.WaitForAllExitedAsync(TimeSpan.FromMilliseconds(200)));

        job.ExitAll();
        Assert.True(await group.WaitForAllExitedAsync(TimeSpan.FromMilliseconds(500)));
    }

    [Fact]
    public void Disposing_the_group_closes_the_job_handle_kill_on_close_backstop()
    {
        var job = new FakeJob(10);
        var group = new JobOwnedProcessGroup("backend", job, new FakeRoot(10));
        group.Dispose();
        group.Dispose();

        Assert.True(job.Disposed);
        Assert.Empty(group.ActiveProcessIds());
        group.TerminateAll();
        Assert.Equal(0, job.TerminateCalls);
    }

    [Fact]
    public async Task Launcher_registers_every_child_with_its_own_group()
    {
        var h = new Harness();
        await h.Controller.StartAsync();
        Assert.Equal(new[] { "backend", "frontend" }, h.Launcher.Started.Select(s => s.Label));
        Assert.NotSame(h.Backend.Job, h.Frontend.Job);
        Assert.NotEmpty(h.Controller.BackendProcessIds);
        Assert.NotEmpty(h.Controller.FrontendProcessIds);
    }

    [Fact]
    public void Platform_launcher_on_windows_provides_kill_on_close()
    {
        var launcher = SessionFactory.CreatePlatformLauncher();
        Assert.Equal(OperatingSystem.IsWindows(), launcher.ProvidesKillOnClose);
    }
}

public sealed class WindowsCommandLineTests
{
    [Theory]
    [InlineData("plain", "plain")]
    [InlineData("with space", "\"with space\"")]
    [InlineData("", "\"\"")]
    [InlineData("quote\"inside", "\"quote\\\"inside\"")]
    [InlineData(@"C:\path with space\", "\"C:\\path with space\\\\\"")]
    public void Arguments_follow_msvcrt_quoting(string input, string expected)
    {
        Assert.Equal(expected, WindowsCommandLine.QuoteArgument(input));
    }

    [Fact]
    public void Environment_block_is_sorted_and_double_null_terminated_by_marshalling()
    {
        var block = WindowsCommandLine.EnvironmentBlock(new Dictionary<string, string>
        {
            ["b"] = "2",
            ["A"] = "1",
            ["=C:"] = @"C:\",
            ["bad=key"] = "x",
        });
        Assert.Equal("=C:=C:\\\0A=1\0b=2\0", block);
    }

    [Fact]
    public void Raw_cmd_arguments_are_used_verbatim_for_npm_cmd()
    {
        var spec = new ProcessSpec("frontend", @"C:\Windows\System32\cmd.exe", Array.Empty<string>(), @"C:\repo\frontend",
            new Dictionary<string, string>(), "o", "e", "/d /s /c \"\"C:\\Program Files\\nodejs\\npm.cmd\" run start\"");
        Assert.Equal("C:\\Windows\\System32\\cmd.exe /d /s /c \"\"C:\\Program Files\\nodejs\\npm.cmd\" run start\"",
            WindowsCommandLine.Build(spec));
    }
}

/// <summary>Real Job Object behaviour. Runs on windows-latest CI.</summary>
public sealed class WindowsJobObjectIntegrationTests
{
    private static ProcessSpec PingTree(string label, int seconds)
    {
        var logs = Path.Combine(Path.GetTempPath(), "sh-job-tests", Guid.NewGuid().ToString("N"));
        return new ProcessSpec(label, System.Environment.GetEnvironmentVariable("ComSpec") ?? @"C:\Windows\System32\cmd.exe",
            Array.Empty<string>(), Path.GetTempPath(), ChildEnvironment.CurrentProcess(),
            Path.Combine(logs, "out.log"), Path.Combine(logs, "err.log"), $"/d /c ping -n {seconds} 127.0.0.1");
    }

    private static Process StartUnrelatedPing() => Process.Start(new ProcessStartInfo("ping", "-n 120 127.0.0.1")
    {
        UseShellExecute = false,
        CreateNoWindow = true,
        RedirectStandardOutput = true,
    })!;

    private static async Task<IReadOnlyList<int>> WaitForMembers(IOwnedProcessGroup group, int count)
    {
        for (var i = 0; i < 100 && group.ActiveProcessIds().Count < count; i++)
        {
            await Task.Delay(100);
        }
        return group.ActiveProcessIds();
    }

    private static bool IsAlive(int pid)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            return !p.HasExited;
        }
        catch (ArgumentException)
        {
            return false;
        }
    }

    private static async Task<bool> WaitGone(IEnumerable<int> pids, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (pids.All(pid => !IsAlive(pid)))
            {
                return true;
            }
            await Task.Delay(100);
        }
        return pids.All(pid => !IsAlive(pid));
    }

    [WindowsFact]
    public void Job_object_is_configured_kill_on_job_close()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var job = new WindowsJobObject();
        Assert.True(job.KillOnClose);
    }

    [WindowsFact]
    public async Task Child_and_grandchild_are_inside_the_job()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var group = new WindowsJobProcessLauncher().Start(PingTree("probe", 60));
        var members = await WaitForMembers(group, 2);
        Assert.Contains(group.RootProcessId, members);
        Assert.True(members.Count >= 2, $"expected cmd + ping in job, saw [{string.Join(',', members)}]");
    }

    [WindowsFact]
    public async Task Closing_the_job_handle_kills_its_tree_but_not_unrelated_processes()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var unrelated = StartUnrelatedPing();
        var group = new WindowsJobProcessLauncher().Start(PingTree("probe", 120));
        var members = await WaitForMembers(group, 2);
        Assert.DoesNotContain(unrelated.Id, members);

        group.Dispose();

        Assert.True(await WaitGone(members, TimeSpan.FromSeconds(10)), "job members survived job handle close");
        Assert.False(unrelated.HasExited, "an unrelated process was terminated");
        unrelated.Kill();
    }

    [WindowsFact]
    public async Task Terminate_all_stops_exactly_the_group()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var unrelated = StartUnrelatedPing();
        using var group = new WindowsJobProcessLauncher().Start(PingTree("probe", 120));
        var members = await WaitForMembers(group, 2);

        group.TerminateAll();

        Assert.True(await group.WaitForAllExitedAsync(TimeSpan.FromSeconds(10)));
        Assert.True(await WaitGone(members, TimeSpan.FromSeconds(5)));
        Assert.False(unrelated.HasExited);
        unrelated.Kill();
    }

    [WindowsFact]
    public async Task Forcibly_killing_the_controller_process_kills_its_job_children()
    {
        if (!OperatingSystem.IsWindows()) return;
        // Equivalent of Task Manager "End task" (TerminateProcess) on SportsHedge.exe.
        using var unrelated = StartUnrelatedPing();
        using var host = Process.Start(new ProcessStartInfo(TestPaths.DotnetHost)
        {
            ArgumentList = { TestPaths.TestHostDll, "job-probe" },
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
        })!;
        var line = await host.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(30));
        Assert.NotNull(line);
        Assert.StartsWith("JOB_PIDS=", line, StringComparison.Ordinal);
        var children = line!["JOB_PIDS=".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToArray();
        Assert.True(children.Length >= 2, line);
        Assert.All(children, pid => Assert.True(IsAlive(pid)));

        host.Kill();
        await host.WaitForExitAsync();

        Assert.True(await WaitGone(children, TimeSpan.FromSeconds(10)), $"orphans survived controller kill: {line}");
        Assert.False(unrelated.HasExited, "an unrelated process was terminated");
        unrelated.Kill();
    }
}
