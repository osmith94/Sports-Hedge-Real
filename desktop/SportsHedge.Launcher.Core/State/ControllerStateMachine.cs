namespace SportsHedge.Launcher.Core.State;

public enum ControllerState
{
    NotStarted,
    Starting,
    Running,
    Degraded,
    Stopping,
    Stopped,
    Failed,
    /// <summary>
    /// Shutdown finished but the final verification did not pass: an owned
    /// process could not be confirmed gone, or port 8000/3000 is still held.
    /// </summary>
    ShutdownIncomplete,
}

public enum ShutdownSource
{
    Ui,
    Tray,
    ControllerExit,
    StartupFailure,
}

public enum ShutdownRequestResult
{
    Accepted,
    AlreadyStopping,
    AlreadyStopped,
}

public sealed record ControllerStatus(ControllerState State, string Message);

/// <summary>
/// Single source of truth for controller lifecycle. UI Exit, tray Exit and
/// Windows/controller exit all enter the same Stopping state exactly once.
/// </summary>
public sealed class ControllerStateMachine
{
    private readonly object _gate = new();
    private ControllerState _state = ControllerState.NotStarted;
    private string _message = "Not started";

    public event Action<ControllerStatus>? Changed;

    public ControllerState State
    {
        get
        {
            lock (_gate)
            {
                return _state;
            }
        }
    }

    public ShutdownSource? LastShutdownSource { get; private set; }

    public ControllerStatus Snapshot()
    {
        lock (_gate)
        {
            return new ControllerStatus(_state, _message);
        }
    }

    public bool TryBeginStartup() => Transition(ControllerState.Starting, "Starting Sports Hedge…", ControllerState.NotStarted);

    public void Progress(string message)
    {
        ControllerStatus? status = null;
        lock (_gate)
        {
            if (_state == ControllerState.Starting)
            {
                _message = message;
                status = new ControllerStatus(_state, message);
            }
        }
        if (status is not null)
        {
            Changed?.Invoke(status);
        }
    }

    public bool MarkRunning(string message = "Sports Hedge running") =>
        Transition(ControllerState.Running, message, ControllerState.Starting, ControllerState.Degraded);

    public bool MarkDegraded(string reason) =>
        Transition(ControllerState.Degraded, reason, ControllerState.Running, ControllerState.Degraded);

    public ShutdownRequestResult TryBeginShutdown(ShutdownSource source, out ControllerState previous)
    {
        ControllerStatus status;
        lock (_gate)
        {
            previous = _state;
            switch (_state)
            {
                case ControllerState.Stopping:
                    return ShutdownRequestResult.AlreadyStopping;
                case ControllerState.Stopped:
                case ControllerState.Failed:
                case ControllerState.ShutdownIncomplete:
                    return ShutdownRequestResult.AlreadyStopped;
                case ControllerState.NotStarted:
                    LastShutdownSource = source;
                    _state = ControllerState.Stopped;
                    _message = "Stopped before startup";
                    status = new ControllerStatus(_state, _message);
                    break;
                default:
                    LastShutdownSource = source;
                    _state = ControllerState.Stopping;
                    _message = source == ShutdownSource.StartupFailure ? "Startup failed — cleaning up" : "Stopping Sports Hedge…";
                    status = new ControllerStatus(_state, _message);
                    break;
            }
        }
        Changed?.Invoke(status);
        return ShutdownRequestResult.Accepted;
    }

    /// <summary>Clean <see cref="ControllerState.Stopped"/> only when the final shutdown verification passed.</summary>
    public void MarkFinished(bool failed, bool verified, string message) =>
        Transition(
            failed ? ControllerState.Failed : verified ? ControllerState.Stopped : ControllerState.ShutdownIncomplete,
            message,
            ControllerState.Stopping);

    private bool Transition(ControllerState target, string message, params ControllerState[] allowedFrom)
    {
        ControllerStatus status;
        lock (_gate)
        {
            if (!allowedFrom.Contains(_state))
            {
                return false;
            }
            _state = target;
            _message = message;
            status = new ControllerStatus(target, message);
        }
        Changed?.Invoke(status);
        return true;
    }
}
