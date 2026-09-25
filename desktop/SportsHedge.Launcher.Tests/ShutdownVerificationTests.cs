using System.Diagnostics;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Processes.Windows;
using SportsHedge.Launcher.Core.ProcessEnvironment;
using SportsHedge.Launcher.Core.State;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class SystemOwnedProcessProbeTests
{
    private static Process StartWaitingOnStdin()
    {
        // `git hash-object --stdin` blocks until stdin closes: a harmless,
        // cross-platform child whose lifetime the test controls.
        var psi = new ProcessStartInfo("git")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
        };
        psi.ArgumentList.Add("hash-object");
        psi.ArgumentList.Add("--stdin");
        return Process.Start(psi)!;
    }

    [Fact]
    public async Task Captured_process_is_alive_until_it_exits()
    {
        var probe = new SystemOwnedProcessProbe();
        using var child = StartWaitingOnStdin();
        var identity = probe.Capture(child.Id);

        Assert.NotNull(identity);
        Assert.NotNull(identity!.StartTimeUtc);
        Assert.True(probe.IsAlive(identity));

        child.StandardInput.Close();
        await child.WaitForExitAsync();

        Assert.False(probe.IsAlive(identity));
        Assert.Null(probe.Capture(child.Id));
    }

    [Fact]
    public void A_reused_pid_with_a_different_start_time_is_not_the_owned_process()
    {
        var probe = new SystemOwnedProcessProbe();
        var self = probe.Capture(System.Environment.ProcessId)!;

        Assert.True(probe.IsAlive(self));
        Assert.False(probe.IsAlive(self with { StartTimeUtc = self.StartTimeUtc!.Value.AddHours(-1) }));
    }

    [Fact]
    public void Unknown_pid_is_not_alive()
    {
        var probe = new SystemOwnedProcessProbe();
        Assert.Null(probe.Capture(int.MaxValue - 7));
        Assert.False(probe.IsAlive(new OwnedProcessIdentity(int.MaxValue - 7, null)));
    }

    [WindowsFact]
    public async Task Job_members_captured_before_disposal_are_verified_gone_after_job_close()
    {
        if (!OperatingSystem.IsWindows()) return;
        var logs = Path.Combine(Path.GetTempPath(), "sh-verify-tests", Guid.NewGuid().ToString("N"));
        var spec = new ProcessSpec("probe", System.Environment.GetEnvironmentVariable("ComSpec") ?? @"C:\Windows\System32\cmd.exe",
            Array.Empty<string>(), Path.GetTempPath(), ChildEnvironment.CurrentProcess(),
            Path.Combine(logs, "out.log"), Path.Combine(logs, "err.log"), "/d /c ping -n 120 127.0.0.1");
        var probe = new SystemOwnedProcessProbe();
        var group = new WindowsJobProcessLauncher().Start(spec);
        for (var i = 0; i < 100 && group.ActiveProcessIds().Count < 2; i++)
        {
            await Task.Delay(100);
        }
        var identities = group.ActiveProcessIds().Select(probe.Capture).OfType<OwnedProcessIdentity>().ToList();
        Assert.True(identities.Count >= 2);

        group.Dispose();

        Assert.Empty(group.ActiveProcessIds());
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (identities.Any(probe.IsAlive) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(100);
        }
        Assert.DoesNotContain(identities, probe.IsAlive);
    }
}

public sealed class ShutdownOutcomeMessageTests
{
    private static ShutdownReport Report(ShutdownVerification verification, int[]? remaining = null, PortOccupant[]? foreign = null) =>
        new(ShutdownSource.Ui, false, true, false, true, remaining ?? Array.Empty<int>(),
            foreign?.Select(f => f.Port).ToArray() ?? Array.Empty<int>(), verification, new[] { 1, 2 }, foreign ?? Array.Empty<PortOccupant>());

    [Fact]
    public void Only_a_verified_shutdown_says_sports_hedge_has_stopped()
    {
        Assert.Equal("Sports Hedge has stopped", OperatorMessages.ShutdownOutcome(Report(ShutdownVerification.Verified)));
        Assert.True(Report(ShutdownVerification.Verified).CleanStop);
        Assert.False(Report(ShutdownVerification.OwnedProcessesRemain, new[] { 7 }).CleanStop);
        Assert.False(Report(ShutdownVerification.ForeignPortOccupant).CleanStop);
        Assert.False((Report(ShutdownVerification.Verified) with { Failed = true }).CleanStop);
    }

    [Fact]
    public void Owned_orphans_and_foreign_occupants_have_distinct_truthful_messages()
    {
        var owned = OperatorMessages.ShutdownOutcome(Report(ShutdownVerification.OwnedProcessesRemain, new[] { 41, 42 }));
        Assert.StartsWith("Shutdown incomplete", owned);
        Assert.Contains("PID 41, 42", owned);

        var foreign = OperatorMessages.ShutdownOutcome(Report(ShutdownVerification.ForeignPortOccupant,
            foreign: new[] { new PortOccupant(8000, 99, "PID 99 (python)") }));
        Assert.Contains("processes have stopped", foreign);
        Assert.Contains("port 8000 is in use by another process (PID 99 (python))", foreign);
        Assert.Contains("not terminated", foreign);
        Assert.DoesNotContain("Shutdown incomplete", foreign);
    }

    [Fact]
    public void Incomplete_shutdown_has_its_own_terminal_tray_state()
    {
        Assert.Equal("Sports Hedge — Shutdown incomplete", OperatorMessages.TrayText(ControllerState.ShutdownIncomplete));
        Assert.False(OperatorMessages.CanOpenBrowser(ControllerState.ShutdownIncomplete));
    }
}
