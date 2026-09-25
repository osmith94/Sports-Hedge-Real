using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.SingleInstance;

namespace SportsHedge.Launcher;

/// <summary>
/// Named auto-reset event: a second SportsHedge.exe sets it, the running
/// controller opens the browser. Only the primary instance creates it.
/// </summary>
internal sealed class WindowsInstanceActivation : IInstanceActivation, IDisposable
{
    private EventWaitHandle? _event;
    private RegisteredWaitHandle? _registration;

    public bool SignalPrimary()
    {
        try
        {
            if (EventWaitHandle.TryOpenExisting(LauncherConstants.ActivationEventName, out var existing))
            {
                using (existing)
                {
                    return existing.Set();
                }
            }
        }
        catch (UnauthorizedAccessException)
        {
        }
        return false;
    }

    public void Listen(Action onActivated)
    {
        _event = new EventWaitHandle(false, EventResetMode.AutoReset, LauncherConstants.ActivationEventName);
        _registration = ThreadPool.RegisterWaitForSingleObject(
            _event, (_, _) => onActivated(), null, Timeout.Infinite, executeOnlyOnce: false);
    }

    public void Dispose()
    {
        _registration?.Unregister(null);
        _event?.Dispose();
    }
}

internal static class BrowserLauncher
{
    /// <summary>
    /// Hands the URL to the user's default browser. The controller does not own
    /// or track the browser process and never terminates it.
    /// </summary>
    public static void Open(string url)
    {
        try
        {
            using var _ = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (System.ComponentModel.Win32Exception)
        {
        }
    }
}
