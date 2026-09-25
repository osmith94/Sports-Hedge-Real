using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.Ports;
using SportsHedge.Launcher.Core.State;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class SessionControllerTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    [Fact]
    public async Task Startup_matching_build_skips_rebuild_and_starts_backend_before_frontend()
    {
        var h = new Harness(markerMatches: true);

        var result = await h.Controller.StartAsync();

        Assert.True(result.Success, result.Reason);
        Assert.Equal(ControllerState.Running, h.Controller.State.State);
        Assert.Equal(0, h.Builder.Builds);
        Assert.Equal(new[] { "dependencies", "start:backend", "start:frontend" }, h.Journal.Events);
        Assert.True(h.Log.Contains("rebuild=False reason=Match"));
        Assert.NotNull(h.Ipc.Handler);
    }

    [Fact]
    public async Task Startup_sha_mismatch_rebuilds_frontend_before_launching_services()
    {
        var h = new Harness(markerMatches: false);
        TestLayouts.WriteBuild(h.Layout, "def4560000000000000000000000000000000000");

        var result = await h.Controller.StartAsync();

        Assert.True(result.Success, result.Reason);
        Assert.Equal(1, h.Builder.Builds);
        Assert.True(h.Journal.IndexOf("build") < h.Journal.IndexOf("start:backend"));
        Assert.True(h.Log.Contains("reason=ShaMismatch"));
        Assert.True(h.Log.Contains("Frontend built for SHA: def456"));
    }

    [Fact]
    public async Task Startup_missing_build_triggers_rebuild()
    {
        var h = new Harness(markerMatches: false);

        var result = await h.Controller.StartAsync();

        Assert.True(result.Success, result.Reason);
        Assert.Equal(1, h.Builder.Builds);
        Assert.True(h.Log.Contains("reason=MissingMarker"));
    }

    [Fact]
    public async Task Failed_frontend_build_refuses_startup_and_never_starts_stale_frontend()
    {
        var h = new Harness(markerMatches: false);
        TestLayouts.WriteBuild(h.Layout, "def4560000000000000000000000000000000000");
        h.Builder.Fail = true;

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Frontend build", result.FailedComponent);
        Assert.Contains("NOT started", result.Reason);
        Assert.Empty(h.Launcher.Started);
        Assert.Equal(ControllerState.Failed, h.Controller.State.State);
        Assert.False(File.Exists(h.Layout.FrontendBuildMarkerPath));
        var report = await h.Controller.Completion.WaitAsync(Wait);
        Assert.True(report.Failed);
    }

    [Fact]
    public async Task Npm_ci_failure_refuses_startup_before_any_build_or_service()
    {
        var h = new Harness(markerMatches: false);
        h.Builder.FailDependencies = true;

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Frontend dependencies", result.FailedComponent);
        Assert.Contains("npm ci failed", result.Reason);
        Assert.Contains("NOT built or started", result.Reason);
        Assert.Equal(0, h.Builder.Builds);
        Assert.Empty(h.Launcher.Started);
        Assert.Equal(ControllerState.Failed, h.Controller.State.State);
        Assert.True((await h.Controller.Completion.WaitAsync(Wait)).Failed);
    }

    [Fact]
    public async Task Unknown_port_occupant_refuses_startup_without_terminating_anything()
    {
        var h = new Harness();
        h.Ports.Occupied[8000] = new PortStatus(8000, true, 4242, "python");

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Ports", result.FailedComponent);
        Assert.Contains("port 8000 is already in use by another process (PID 4242 (python))", result.Reason);
        Assert.Contains("No processes were terminated", result.Reason);
        Assert.Empty(h.Launcher.Started);
        Assert.DoesNotContain(h.Journal.Events, e => e.EndsWith(":terminate", StringComparison.Ordinal));
        Assert.Equal(0, h.BackendShutdown.Calls);
    }

    [Fact]
    public async Task Frontend_port_occupied_is_refused_too()
    {
        var h = new Harness();
        h.Ports.Occupied[3000] = new PortStatus(3000, true);

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Contains("port 3000", result.Reason);
        Assert.Empty(h.Launcher.Started);
    }

    [Fact]
    public async Task Backend_failure_cleans_up_owned_backend_and_never_starts_frontend()
    {
        var h = new Harness();
        h.Health.Backend = () => HealthResult.Fail("connection refused");

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Backend", result.FailedComponent);
        Assert.DoesNotContain("start:frontend", h.Journal.Events);
        Assert.Contains("backend:dispose", h.Journal.Events);
        Assert.Empty(h.Backend.ActiveProcessIds());
        Assert.Equal(ControllerState.Failed, h.Controller.State.State);
    }

    [Fact]
    public async Task Backend_exiting_during_startup_is_reported_with_exit_code()
    {
        var h = new Harness();
        h.Health.Backend = () =>
        {
            h.Backend.ExitNaturally(3);
            return HealthResult.Fail("not yet");
        };

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Contains("exited with code 3", result.Reason);
    }

    [Fact]
    public async Task Frontend_failure_after_backend_started_cleans_up_backend_gracefully()
    {
        var h = new Harness();
        h.Health.Frontend = () => HealthResult.Fail("interface not ready");

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Equal(1, h.BackendShutdown.Calls);
        Assert.Contains("frontend:terminate", h.Journal.Events);
        Assert.Contains("backend:dispose", h.Journal.Events);
        Assert.Contains("frontend:dispose", h.Journal.Events);
        Assert.True(h.Ipc.Stopped);
    }

    [Fact]
    public async Task Graceful_shutdown_transitions_and_order_backend_then_frontend()
    {
        var h = new Harness();
        var states = new List<ControllerState>();
        h.Controller.State.Changed += s => { lock (states) states.Add(s.State); };
        Assert.True((await h.Controller.StartAsync()).Success);

        Assert.Equal(ShutdownRequestResult.Accepted, h.Controller.RequestShutdown(ShutdownSource.Tray));
        var report = await h.Controller.Completion.WaitAsync(Wait);

        Assert.Equal(ShutdownSource.Tray, report.Source);
        Assert.True(report.BackendGraceful);
        Assert.False(report.BackendForced);
        Assert.True(report.FrontendStopped);
        Assert.Empty(report.RemainingProcessIds);
        Assert.Empty(report.PortsStillListening);
        Assert.Equal(ControllerState.Stopped, h.Controller.State.State);
        lock (states)
        {
            Assert.Equal(ControllerState.Running, states[^3]);
            Assert.Equal(ControllerState.Stopping, states[^2]);
            Assert.Equal(ControllerState.Stopped, states[^1]);
        }
        var events = h.Journal.Events.ToList();
        Assert.True(events.IndexOf("backend:graceful-request") < events.IndexOf("frontend:terminate"));
        Assert.DoesNotContain("backend:terminate", events);
        Assert.True(events.IndexOf("frontend:terminate") < events.IndexOf("backend:dispose"));
        Assert.Equal(h.Secrets.BackendShutdownToken, Assert.Single(h.BackendShutdown.Tokens));
        Assert.True(h.Ipc.Stopped);
    }

    [Fact]
    public async Task Duplicate_shutdown_requests_are_idempotent_across_ui_and_tray()
    {
        var h = new Harness();
        Assert.True((await h.Controller.StartAsync()).Success);

        var first = h.Controller.RequestShutdown(ShutdownSource.Ui);
        var second = h.Controller.RequestShutdown(ShutdownSource.Tray);
        var third = h.Controller.RequestShutdown(ShutdownSource.Ui);
        await h.Controller.Completion.WaitAsync(Wait);
        var afterStop = h.Controller.RequestShutdown(ShutdownSource.Tray);

        Assert.Equal(ShutdownRequestResult.Accepted, first);
        Assert.Equal(ShutdownRequestResult.AlreadyStopping, second);
        Assert.Equal(ShutdownRequestResult.AlreadyStopping, third);
        Assert.Equal(ShutdownRequestResult.AlreadyStopped, afterStop);
        Assert.Equal(1, h.BackendShutdown.Calls);
        Assert.Equal(ShutdownSource.Ui, h.Controller.State.LastShutdownSource);
    }

    [Fact]
    public async Task Graceful_timeout_falls_back_to_terminating_only_the_owned_backend_group()
    {
        var h = new Harness();
        h.BackendShutdown.OnRequest = null;
        Assert.True((await h.Controller.StartAsync()).Success);

        h.Controller.RequestShutdown(ShutdownSource.Ui);
        var report = await h.Controller.Completion.WaitAsync(Wait);

        Assert.False(report.BackendGraceful);
        Assert.True(report.BackendForced);
        Assert.Equal(1, h.Backend.Job.TerminateCalls);
        Assert.True(h.Log.Contains("backend_forced_cleanup"));
        Assert.Empty(report.RemainingProcessIds);
    }

    [Fact]
    public async Task Backend_refusing_graceful_request_is_forced_via_its_job_only()
    {
        var h = new Harness();
        h.BackendShutdown.Accept = false;
        h.BackendShutdown.OnRequest = null;
        Assert.True((await h.Controller.StartAsync()).Success);

        h.Controller.RequestShutdown(ShutdownSource.Tray);
        var report = await h.Controller.Completion.WaitAsync(Wait);

        Assert.True(report.BackendForced);
        Assert.Equal(1, h.Backend.Job.TerminateCalls);
        Assert.Equal(1, h.Frontend.Job.TerminateCalls);
    }

    [Fact]
    public async Task Foreign_listener_left_on_port_is_reported_and_not_killed()
    {
        var h = new Harness();
        Assert.True((await h.Controller.StartAsync()).Success);
        h.Ports.Occupied[3000] = new PortStatus(3000, true, 999, "node");

        h.Controller.RequestShutdown(ShutdownSource.Ui);
        var report = await h.Controller.Completion.WaitAsync(Wait);

        Assert.Equal(new[] { 3000 }, report.PortsStillListening);
        Assert.True(h.Log.Contains("not terminated because it is not owned by this controller"));
    }

    [Fact]
    public async Task Shutdown_requested_during_frontend_build_cancels_and_cleans_up()
    {
        var h = new Harness(markerMatches: false);
        h.Builder.Gate = new TaskCompletionSource();
        var start = h.Controller.StartAsync();
        while (h.Builder.Builds == 0)
        {
            await Task.Delay(10);
        }

        Assert.Equal(ShutdownRequestResult.Accepted, h.Controller.RequestShutdown(ShutdownSource.Tray));
        var result = await start.WaitAsync(Wait);

        Assert.True(result.StoppedByRequest);
        Assert.Empty(h.Launcher.Started);
        Assert.Equal(ControllerState.Stopped, h.Controller.State.State);
    }

    [Fact]
    public async Task Ui_ipc_shutdown_uses_the_same_state_machine_and_requires_the_token()
    {
        var h = new Harness();
        Assert.True((await h.Controller.StartAsync()).Success);

        var missing = h.IpcRequest("shutdown", null);
        var wrong = h.IpcRequest("shutdown", "x" + h.Secrets.ControllerToken[1..]);
        Assert.Contains("\"unauthorized\"", missing);
        Assert.Contains("\"unauthorized\"", wrong);
        Assert.Equal(ControllerState.Running, h.Controller.State.State);

        var accepted = h.IpcRequest("shutdown", h.Secrets.ControllerToken);
        Assert.Contains("\"accepted\"", accepted);
        Assert.DoesNotContain(h.Secrets.ControllerToken, accepted);
        await h.Controller.Completion.WaitAsync(Wait);
        Assert.Equal(ShutdownSource.Ui, h.Controller.State.LastShutdownSource);
        Assert.True(h.Log.Contains("shutdown_complete source=Ui"));
    }

    [Fact]
    public async Task Backend_crash_marks_degraded_and_never_claims_healthy()
    {
        var h = new Harness();
        var degraded = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        h.Controller.State.Changed += s => { if (s.State == ControllerState.Degraded) degraded.TrySetResult(s.Message); };
        Assert.True((await h.Controller.StartAsync()).Success);

        h.Backend.ExitNaturally(137);
        var message = await degraded.Task.WaitAsync(Wait);

        Assert.Contains("Backend process exited unexpectedly (exit code 137)", message);
        Assert.Contains("NOT healthy", message);
        await Task.Delay(200);
        Assert.Equal(ControllerState.Degraded, h.Controller.State.State);
        Assert.True(h.Log.Contains("child_exited component=backend"));
        Assert.DoesNotContain("frontend:terminate", h.Journal.Events);

        h.Controller.RequestShutdown(ShutdownSource.Tray);
        var report = await h.Controller.Completion.WaitAsync(Wait);
        Assert.Empty(report.RemainingProcessIds);
    }

    [Fact]
    public async Task Frontend_crash_marks_degraded()
    {
        var h = new Harness();
        var degraded = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        h.Controller.State.Changed += s => { if (s.State == ControllerState.Degraded) degraded.TrySetResult(s.Message); };
        Assert.True((await h.Controller.StartAsync()).Success);

        h.Frontend.ExitNaturally(1);
        var message = await degraded.Task.WaitAsync(Wait);

        Assert.Contains("Interface process exited unexpectedly", message);
        Assert.Equal(ControllerState.Degraded, h.Controller.State.State);
        Assert.NotEmpty(h.Backend.ActiveProcessIds());
    }

    [Fact]
    public async Task Failing_health_checks_degrade_then_recover()
    {
        var h = new Harness();
        var healthy = true;
        h.Health.Backend = () => healthy ? HealthResult.Ok("ok") : HealthResult.Fail("timeout");
        var seen = new List<ControllerState>();
        h.Controller.State.Changed += s => { lock (seen) seen.Add(s.State); };
        Assert.True((await h.Controller.StartAsync()).Success);

        healthy = false;
        await WaitFor(() => h.Controller.State.State == ControllerState.Degraded);
        healthy = true;
        await WaitFor(() => h.Controller.State.State == ControllerState.Running);

        lock (seen)
        {
            Assert.Contains(ControllerState.Degraded, seen);
        }
        Assert.True(h.Log.Contains("health_recovered"));
    }

    [Fact]
    public async Task Prerequisite_or_git_failure_is_reported_before_any_child_starts()
    {
        var h = new Harness();
        h.Prereqs.Problem = "Python venv not found";
        var result = await h.Controller.StartAsync();
        Assert.Equal("Prerequisites", result.FailedComponent);
        Assert.Empty(h.Launcher.Started);

        var g = new Harness();
        g.Git.Throw = true;
        var gitResult = await g.Controller.StartAsync();
        Assert.Equal("Git", gitResult.FailedComponent);
        Assert.Empty(g.Launcher.Started);
    }

    [Fact]
    public async Task Startup_failure_message_contains_component_reason_logs_and_git()
    {
        var h = new Harness();
        h.Ports.Occupied[8000] = new PortStatus(8000, true);
        var result = await h.Controller.StartAsync();

        var message = OperatorMessages.StartupFailure(result, h.Layout.LogsDir);

        Assert.Contains("Failed component: Ports", message);
        Assert.Contains("port 8000", message);
        Assert.Contains(h.Layout.LogsDir, message);
        Assert.Contains(h.Git.Identity.Sha, message);
        Assert.Contains("(main)", message);
    }

    private static async Task WaitFor(Func<bool> condition)
    {
        var deadline = DateTime.UtcNow + Wait;
        while (!condition())
        {
            if (DateTime.UtcNow > deadline)
            {
                throw new TimeoutException("condition not met");
            }
            await Task.Delay(20);
        }
    }
}
