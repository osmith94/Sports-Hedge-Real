using System.Diagnostics;

namespace SportsHedge.Launcher.Core.Processes;

/// <summary>
/// A process this controller owned, identified by PID and start time so a
/// PID later reused by an unrelated process is never mistaken for it.
/// </summary>
public sealed record OwnedProcessIdentity(int Pid, DateTime? StartTimeUtc);

/// <summary>
/// Read-only OS liveness checks for PIDs captured from this controller's own
/// Job Objects. Used after the jobs are closed, when the groups themselves can
/// no longer report members. Nothing here can terminate a process.
/// </summary>
public interface IOwnedProcessProbe
{
    /// <summary>Null when the process has already exited.</summary>
    OwnedProcessIdentity? Capture(int pid);

    bool IsAlive(OwnedProcessIdentity identity);
}

public sealed class SystemOwnedProcessProbe : IOwnedProcessProbe
{
    public OwnedProcessIdentity? Capture(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return process.HasExited ? null : new OwnedProcessIdentity(pid, TryStartTime(process));
        }
        catch (ArgumentException)
        {
            return null;
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    public bool IsAlive(OwnedProcessIdentity identity)
    {
        try
        {
            using var process = Process.GetProcessById(identity.Pid);
            if (process.HasExited)
            {
                return false;
            }
            var started = TryStartTime(process);
            // Same PID but a different start time is a reused PID, not ours.
            return identity.StartTimeUtc is null || started is null || started == identity.StartTimeUtc;
        }
        catch (ArgumentException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
    }

    private static DateTime? TryStartTime(Process process)
    {
        try
        {
            return process.StartTime.ToUniversalTime();
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            return null;
        }
    }
}
