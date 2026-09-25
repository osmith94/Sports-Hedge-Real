namespace SportsHedge.Launcher.Core.SingleInstance;

/// <summary>
/// Named-mutex single-instance guard. The OS releases the mutex if the owning
/// controller dies, so a crashed controller never blocks the next launch.
/// </summary>
public sealed class SingleInstanceGuard : IDisposable
{
    private Mutex? _mutex;

    private SingleInstanceGuard(Mutex? mutex)
    {
        _mutex = mutex;
    }

    public bool IsPrimary => _mutex is not null;

    public static SingleInstanceGuard Acquire(string name)
    {
        var mutex = new Mutex(initiallyOwned: true, name, out var createdNew);
        if (createdNew)
        {
            return new SingleInstanceGuard(mutex);
        }
        mutex.Dispose();
        return new SingleInstanceGuard(null);
    }

    public void Dispose()
    {
        var mutex = Interlocked.Exchange(ref _mutex, null);
        if (mutex is null)
        {
            return;
        }
        try
        {
            mutex.ReleaseMutex();
        }
        catch (ApplicationException)
        {
            // Released from a different thread than the owner; disposing the
            // handle still frees the name.
        }
        mutex.Dispose();
    }
}

public interface IInstanceActivation
{
    /// <summary>Ask the running controller to open the browser. False when no controller answered.</summary>
    bool SignalPrimary();
}

public enum SecondaryLaunchOutcome
{
    SignalledPrimary,
    OpenedBrowserDirectly,
}

/// <summary>What a second SportsHedge.exe does: never start children, open the existing UI, exit.</summary>
public static class SecondaryInstance
{
    public static SecondaryLaunchOutcome Handle(IInstanceActivation activation, Action<string> openBrowser)
    {
        if (activation.SignalPrimary())
        {
            return SecondaryLaunchOutcome.SignalledPrimary;
        }
        openBrowser(LauncherConstants.AppUrl);
        return SecondaryLaunchOutcome.OpenedBrowserDirectly;
    }
}
