using SportsHedge.Launcher.Core.Backend;
using SportsHedge.Launcher.Core.Build;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.Ipc;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Ports;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Runtime;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.State;

namespace SportsHedge.Launcher.Core;

public sealed record SessionOptions
{
    public TimeSpan BackendHealthTimeout { get; init; } = TimeSpan.FromSeconds(180);
    public TimeSpan FrontendHealthTimeout { get; init; } = TimeSpan.FromSeconds(120);
    public TimeSpan HealthPollInterval { get; init; } = TimeSpan.FromMilliseconds(500);
    public TimeSpan BackendGracefulTimeout { get; init; } = TimeSpan.FromSeconds(30);
    public TimeSpan ForcedExitTimeout { get; init; } = TimeSpan.FromSeconds(10);
    public TimeSpan FrontendStopTimeout { get; init; } = TimeSpan.FromSeconds(15);
    public TimeSpan PortReleaseTimeout { get; init; } = TimeSpan.FromSeconds(15);
    public TimeSpan MonitorInterval { get; init; } = TimeSpan.FromSeconds(2);
    public int MonitorHealthEveryTicks { get; init; } = 5;
    public int HealthFailuresBeforeDegraded { get; init; } = 3;
}

public sealed record SessionDependencies(
    ILog Log,
    IGitReader Git,
    IPortProbe Ports,
    IHealthProbe Health,
    IProcessGroupLauncher Launcher,
    IFrontendBuilder FrontendBuilder,
    IBackendShutdownClient BackendShutdown,
    IPrerequisites Prerequisites,
    IServiceSpecFactory Specs,
    IControllerIpcHost Ipc);

public sealed record StartupResult(bool Success, string? FailedComponent, string? Reason, GitIdentity? Git, bool StoppedByRequest = false);

public sealed record ShutdownReport(
    ShutdownSource? Source,
    bool Failed,
    bool BackendGraceful,
    bool BackendForced,
    bool FrontendStopped,
    IReadOnlyList<int> RemainingProcessIds,
    IReadOnlyList<int> PortsStillListening);

/// <summary>
/// Owns one Sports Hedge session: preflight, frontend build check, backend and
/// interface children (each in its own kill-on-close group), health, monitoring
/// and the single shutdown path used by UI Exit, tray Exit and Windows exit.
/// </summary>
public sealed class SessionController
{
    private readonly RuntimeLayout _layout;
    private readonly SessionSecrets _secrets;
    private readonly SessionDependencies _deps;
    private readonly SessionOptions _options;
    private readonly ILog _log;
    private readonly CancellationTokenSource _startupCts = new();
    private readonly CancellationTokenSource _monitorCts = new();
    private readonly TaskCompletionSource<ShutdownReport> _completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private int _shutdownRan;
    private IOwnedProcessGroup? _backend;
    private IOwnedProcessGroup? _frontend;
    private Task? _monitor;
    private bool _ipcStarted;

    public SessionController(RuntimeLayout layout, SessionSecrets secrets, SessionDependencies deps, SessionOptions? options = null)
    {
        _layout = layout;
        _secrets = secrets;
        _deps = deps;
        _options = options ?? new SessionOptions();
        _log = deps.Log;
        State.Changed += status => _log.Info($"state={status.State} message=\"{status.Message}\"");
    }

    public ControllerStateMachine State { get; } = new();
    public Task<ShutdownReport> Completion => _completion.Task;
    public GitIdentity? Git { get; private set; }
    public RuntimeLayout Layout => _layout;
    public IReadOnlyList<int> BackendProcessIds => _backend?.ActiveProcessIds() ?? Array.Empty<int>();
    public IReadOnlyList<int> FrontendProcessIds => _frontend?.ActiveProcessIds() ?? Array.Empty<int>();

    public async Task<StartupResult> StartAsync()
    {
        if (!State.TryBeginStartup())
        {
            throw new InvalidOperationException("Session already started");
        }
        var token = _startupCts.Token;
        var component = "Controller";
        _log.Info($"controller_start pid={System.Environment.ProcessId} repo_root=\"{_layout.RepoRoot}\" " +
                  $"kill_on_close={_deps.Launcher.ProvidesKillOnClose} session_id={_secrets.SessionId}");
        try
        {
            component = "Prerequisites";
            var problem = _deps.Prerequisites.Check(_layout);
            if (problem is not null)
            {
                return await FailStartupAsync(component, problem).ConfigureAwait(false);
            }

            component = "Git";
            Git = _deps.Git.ReadIdentity(_layout.RepoRoot);
            _log.Info($"git_identity sha={Git.Sha} branch={Git.Branch} (local checkout as-is; no fetch/pull/switch/reset)");
            _log.Info($"dotenv canonical=\"{_layout.CanonicalDotEnv}\" exists={File.Exists(_layout.CanonicalDotEnv)}");
            if (File.Exists(_layout.LegacyBackendDotEnv))
            {
                _log.Warn($"ignoring leftover {_layout.LegacyBackendDotEnv}; repo-root .env is the only canonical dotenv");
            }

            component = "Ports";
            var preflight = PortPreflight.Evaluate(_deps.Ports);
            if (!preflight.Ok)
            {
                return await FailStartupAsync(component, preflight.Message!).ConfigureAwait(false);
            }

            component = "Frontend build";
            State.Progress("Checking frontend build…");
            var deps = await _deps.FrontendBuilder.EnsureDependenciesAsync(Git, token).ConfigureAwait(false);
            if (!deps.Success)
            {
                return await FailStartupAsync(component, deps.Detail).ConfigureAwait(false);
            }
            var dirty = _deps.Git.IsPathDirty(_layout.RepoRoot, "frontend");
            var decision = EvaluateBuild(dirty);
            _log.Info($"frontend_build_decision {decision.Describe()} frontend_dirty={dirty}");
            if (decision.RebuildRequired)
            {
                State.Progress("Building interface…");
                _log.Info($"frontend_rebuild_start reason={decision.Reason}");
                var build = await _deps.FrontendBuilder.BuildAsync(Git, dirty, token).ConfigureAwait(false);
                _log.Info($"frontend_rebuild_result success={build.Success} detail=\"{build.Detail}\"");
                if (!build.Success)
                {
                    return await FailStartupAsync(component,
                        $"{build.Detail}\nThe previous interface build was NOT started. Sports Hedge remains stopped.").ConfigureAwait(false);
                }
                var after = EvaluateBuild(dirty);
                if (after.RebuildRequired && after.Reason is not (BuildDecisionReason.DirtyAtBuild or BuildDecisionReason.DirtyNow))
                {
                    return await FailStartupAsync(component, $"Frontend build marker is not valid after rebuilding ({after.Reason}).").ConfigureAwait(false);
                }
            }

            component = "Ports";
            preflight = PortPreflight.Evaluate(_deps.Ports);
            if (!preflight.Ok)
            {
                return await FailStartupAsync(component, preflight.Message!).ConfigureAwait(false);
            }

            component = "Controller IPC";
            _deps.Ipc.Start(HandleIpcRequest);
            _ipcStarted = true;

            component = "Backend";
            State.Progress("Starting backend…");
            LogFiles.RollToPrevious(_layout.BackendOutLog);
            LogFiles.RollToPrevious(_layout.BackendErrLog);
            _backend = _deps.Launcher.Start(_deps.Specs.Backend(Git));
            _log.Info($"backend_started root_pid={_backend.RootProcessId} pids=[{string.Join(',', _backend.ActiveProcessIds())}] " +
                      "command=\"python -m sports_hedge.api.desktop_host --host 127.0.0.1 --port 8000\"");
            State.Progress("Waiting for backend…");
            var backendHealth = await WaitHealthyAsync(_backend, "Backend",
                ct => _deps.Health.CheckBackendAsync(Git.Sha, _secrets.SessionId, ct),
                _options.BackendHealthTimeout, _layout.BackendErrLog, token).ConfigureAwait(false);
            _log.Info($"backend_health healthy={backendHealth.Healthy} detail=\"{backendHealth.Detail}\"");
            if (!backendHealth.Healthy)
            {
                return await FailStartupAsync(component, backendHealth.Detail).ConfigureAwait(false);
            }

            component = "Interface";
            State.Progress("Starting interface…");
            LogFiles.RollToPrevious(_layout.FrontendOutLog);
            LogFiles.RollToPrevious(_layout.FrontendErrLog);
            _frontend = _deps.Launcher.Start(_deps.Specs.Frontend(Git));
            _log.Info($"frontend_started root_pid={_frontend.RootProcessId} pids=[{string.Join(',', _frontend.ActiveProcessIds())}] " +
                      "command=\"npm run start -- -H 127.0.0.1 -p 3000\" (production Next.js)");
            State.Progress("Waiting for interface…");
            var frontendHealth = await WaitHealthyAsync(_frontend, "Interface",
                ct => _deps.Health.CheckFrontendAsync(Git.Sha, _secrets.SessionId, includePage: true, ct),
                _options.FrontendHealthTimeout, _layout.FrontendErrLog, token).ConfigureAwait(false);
            _log.Info($"frontend_health healthy={frontendHealth.Healthy} detail=\"{frontendHealth.Detail}\"");
            if (!frontendHealth.Healthy)
            {
                return await FailStartupAsync(component, frontendHealth.Detail).ConfigureAwait(false);
            }

            if (!State.MarkRunning())
            {
                token.ThrowIfCancellationRequested();
                throw new OperationCanceledException("shutdown requested during startup");
            }
            _log.Info($"running url={LauncherConstants.AppUrl} backend_pids=[{string.Join(',', BackendProcessIds)}] " +
                      $"frontend_pids=[{string.Join(',', FrontendProcessIds)}] mode=PAPER execution_enabled=false");
            _monitor = Task.Run(() => MonitorAsync(_monitorCts.Token));
            return new StartupResult(true, null, null, Git);
        }
        catch (OperationCanceledException) when (State.State == ControllerState.Stopping)
        {
            _log.Info($"startup_interrupted_by_shutdown component={component}");
            await RunShutdownAsync(failed: false).ConfigureAwait(false);
            return new StartupResult(false, component, "Shutdown was requested during startup.", Git, StoppedByRequest: true);
        }
        catch (Exception ex)
        {
            return await FailStartupAsync(component, $"{ex.GetType().Name}: {ex.Message}").ConfigureAwait(false);
        }
    }

    public ShutdownRequestResult RequestShutdown(ShutdownSource source)
    {
        var result = State.TryBeginShutdown(source, out var previous);
        if (result != ShutdownRequestResult.Accepted)
        {
            _log.Info($"shutdown_request_ignored source={source} result={result} (idempotent)");
            return result;
        }
        _log.Info($"shutdown_requested source={source} previous_state={previous}");
        switch (previous)
        {
            case ControllerState.NotStarted:
                Interlocked.Exchange(ref _shutdownRan, 1);
                _completion.TrySetResult(new ShutdownReport(source, false, false, false, false, Array.Empty<int>(), Array.Empty<int>()));
                break;
            case ControllerState.Starting:
                _startupCts.Cancel();
                break;
            default:
                _ = Task.Run(() => RunShutdownAsync(failed: false));
                break;
        }
        return result;
    }

    private string HandleIpcRequest(string line) =>
        ControllerIpcProtocol.Handle(line, _secrets.ControllerToken, RequestShutdown, () => State.State, _log.Info);

    private BuildDecision EvaluateBuild(bool dirty) =>
        FrontendBuildDecision.Evaluate(
            Git!.Sha,
            dirty,
            FrontendBuildMarkerStore.Read(_layout.FrontendBuildMarkerPath),
            FrontendBuildMarkerStore.ReadNextBuildId(_layout.NextBuildIdPath));

    private async Task<HealthResult> WaitHealthyAsync(
        IOwnedProcessGroup group,
        string label,
        Func<CancellationToken, Task<HealthResult>> check,
        TimeSpan timeout,
        string errLog,
        CancellationToken token)
    {
        var deadline = DateTime.UtcNow + timeout;
        var last = "no response yet";
        while (true)
        {
            token.ThrowIfCancellationRequested();
            if (group.RootHasExited)
            {
                return HealthResult.Fail($"{label} process exited with code {group.RootExitCode} before becoming healthy. " +
                                         $"See {errLog}.\n{LogFiles.Tail(errLog)}");
            }
            var result = await check(token).ConfigureAwait(false);
            if (result.Healthy)
            {
                return result;
            }
            last = result.Detail;
            if (DateTime.UtcNow >= deadline)
            {
                return HealthResult.Fail($"{label} did not become healthy within {timeout.TotalSeconds:0} seconds: {last}. " +
                                         $"See {errLog}.\n{LogFiles.Tail(errLog)}");
            }
            await Task.Delay(_options.HealthPollInterval, token).ConfigureAwait(false);
        }
    }

    private async Task<StartupResult> FailStartupAsync(string component, string reason)
    {
        _log.Error($"startup_failed component=\"{component}\" reason=\"{reason.Replace('\n', ' ')}\"");
        var result = State.TryBeginShutdown(ShutdownSource.StartupFailure, out _);
        if (result == ShutdownRequestResult.Accepted)
        {
            await RunShutdownAsync(failed: true).ConfigureAwait(false);
        }
        else
        {
            await Completion.ConfigureAwait(false);
        }
        return new StartupResult(false, component, reason, Git);
    }

    private async Task MonitorAsync(CancellationToken token)
    {
        var tick = 0;
        var healthFailures = 0;
        var backendDeadReported = false;
        var frontendDeadReported = false;
        try
        {
            while (!token.IsCancellationRequested)
            {
                await Task.Delay(_options.MonitorInterval, token).ConfigureAwait(false);
                tick++;
                if (_backend!.RootHasExited && !backendDeadReported)
                {
                    backendDeadReported = true;
                    var reason = $"Backend process exited unexpectedly (exit code {_backend.RootExitCode}). " +
                                 @"Sports Hedge is NOT healthy. See logs\desktop-backend.err.log. Use Exit Sports Hedge to clean up.";
                    _log.Error($"child_exited component=backend exit_code={_backend.RootExitCode}");
                    State.MarkDegraded(reason);
                }
                if (_frontend!.RootHasExited && !frontendDeadReported)
                {
                    frontendDeadReported = true;
                    var reason = $"Interface process exited unexpectedly (exit code {_frontend.RootExitCode}). " +
                                 @"Sports Hedge is NOT healthy. See logs\desktop-frontend.err.log. Use Exit Sports Hedge to clean up.";
                    _log.Error($"child_exited component=frontend exit_code={_frontend.RootExitCode}");
                    State.MarkDegraded(reason);
                }
                if (backendDeadReported || frontendDeadReported || tick % _options.MonitorHealthEveryTicks != 0)
                {
                    continue;
                }
                var backend = await _deps.Health.CheckBackendAsync(Git!.Sha, _secrets.SessionId, token).ConfigureAwait(false);
                var frontend = await _deps.Health.CheckFrontendAsync(Git.Sha, _secrets.SessionId, includePage: false, token).ConfigureAwait(false);
                if (backend.Healthy && frontend.Healthy)
                {
                    if (healthFailures >= _options.HealthFailuresBeforeDegraded && State.State == ControllerState.Degraded)
                    {
                        _log.Info("health_recovered");
                        State.MarkRunning("Sports Hedge running (health recovered)");
                    }
                    healthFailures = 0;
                    continue;
                }
                healthFailures++;
                var detail = !backend.Healthy ? $"backend: {backend.Detail}" : $"interface: {frontend.Detail}";
                _log.Warn($"health_check_failed consecutive={healthFailures} {detail}");
                if (healthFailures == _options.HealthFailuresBeforeDegraded)
                {
                    State.MarkDegraded($"Health checks failing ({detail}). Sports Hedge is NOT healthy.");
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private async Task RunShutdownAsync(bool failed)
    {
        if (Interlocked.Exchange(ref _shutdownRan, 1) == 1)
        {
            return;
        }
        var source = State.LastShutdownSource;
        _log.Info($"shutdown_begin source={source}");
        _monitorCts.Cancel();
        if (_monitor is not null)
        {
            try
            {
                await _monitor.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
        }

        var backendGraceful = false;
        var backendForced = false;
        if (_backend is not null)
        {
            if (_backend.ActiveProcessIds().Count > 0)
            {
                if (!_backend.RootHasExited)
                {
                    var request = await _deps.BackendShutdown.RequestShutdownAsync(_secrets.BackendShutdownToken, CancellationToken.None).ConfigureAwait(false);
                    _log.Info($"backend_graceful_request accepted={request.Accepted} detail=\"{request.Detail}\"");
                    if (request.Accepted)
                    {
                        backendGraceful = await _backend.WaitForAllExitedAsync(_options.BackendGracefulTimeout).ConfigureAwait(false);
                        _log.Info($"backend_graceful_exit completed={backendGraceful} exit_code={_backend.RootExitCode} " +
                                  $"timeout_s={_options.BackendGracefulTimeout.TotalSeconds:0}");
                    }
                }
                if (!backendGraceful && _backend.ActiveProcessIds().Count > 0)
                {
                    _log.Warn($"backend_forced_cleanup reason=graceful_shutdown_unavailable_or_timed_out " +
                              $"job_pids=[{string.Join(',', _backend.ActiveProcessIds())}] (terminating only this controller's backend Job Object)");
                    _backend.TerminateAll();
                    backendForced = true;
                    await _backend.WaitForAllExitedAsync(_options.ForcedExitTimeout).ConfigureAwait(false);
                }
            }
            else
            {
                _log.Info($"backend_already_exited exit_code={_backend.RootExitCode}");
            }
        }
        var backendPortListening = _backend is not null
            && await WaitPortReleasedAsync(LauncherConstants.BackendPort).ConfigureAwait(false);

        var frontendStopped = false;
        if (_frontend is not null)
        {
            // Next.js production server keeps no durable state and Windows has
            // no SIGTERM equivalent for it; terminate exactly its Job Object.
            var pids = _frontend.ActiveProcessIds();
            _frontend.TerminateAll();
            frontendStopped = await _frontend.WaitForAllExitedAsync(_options.FrontendStopTimeout).ConfigureAwait(false);
            _log.Info($"frontend_stopped completed={frontendStopped} method=job_terminate job_pids=[{string.Join(',', pids)}]");
        }
        var frontendPortListening = _frontend is not null
            && await WaitPortReleasedAsync(LauncherConstants.FrontendPort).ConfigureAwait(false);

        var remaining = BackendProcessIds.Concat(FrontendProcessIds).ToArray();
        var ports = new List<int>();
        if (backendPortListening) ports.Add(LauncherConstants.BackendPort);
        if (frontendPortListening) ports.Add(LauncherConstants.FrontendPort);
        _log.Info($"shutdown_verify remaining_owned_pids=[{string.Join(',', remaining)}] " +
                  $"port_8000_listening={backendPortListening} port_3000_listening={frontendPortListening}");

        if (_ipcStarted)
        {
            await _deps.Ipc.StopAsync().ConfigureAwait(false);
        }
        _backend?.Dispose();
        _frontend?.Dispose();
        _log.Info("job_objects_released");

        var message = failed ? "Startup failed" : "Sports Hedge has stopped";
        State.MarkFinished(failed, message);
        _log.Info($"shutdown_complete source={source} failed={failed} backend_graceful={backendGraceful} " +
                  $"backend_forced={backendForced} frontend_stopped={frontendStopped}");
        _completion.TrySetResult(new ShutdownReport(source, failed, backendGraceful, backendForced, frontendStopped, remaining, ports));
    }

    /// <summary>Returns true if the port is STILL listening after the timeout. Never kills the occupant.</summary>
    private async Task<bool> WaitPortReleasedAsync(int port)
    {
        var deadline = DateTime.UtcNow + _options.PortReleaseTimeout;
        while (true)
        {
            var status = _deps.Ports.Inspect(port);
            if (!status.Listening)
            {
                return false;
            }
            if (DateTime.UtcNow >= deadline)
            {
                _log.Warn($"port_still_listening port={port} owner=\"{status.OwnerDescription}\" " +
                          "after owned processes exited; not terminated because it is not owned by this controller");
                return true;
            }
            await Task.Delay(TimeSpan.FromMilliseconds(200)).ConfigureAwait(false);
        }
    }
}
