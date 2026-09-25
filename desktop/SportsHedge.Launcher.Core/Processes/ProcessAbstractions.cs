namespace SportsHedge.Launcher.Core.Processes;

/// <param name="RawArguments">
/// Windows only: verbatim argument string for cmd.exe, whose parsing differs
/// from the MSVCRT rules used to quote <paramref name="Arguments"/>.
/// </param>
public sealed record ProcessSpec(
    string Label,
    string FileName,
    IReadOnlyList<string> Arguments,
    string WorkingDirectory,
    IReadOnlyDictionary<string, string> Environment,
    string StdoutLog,
    string StderrLog,
    string? RawArguments = null)
{
    public string DisplayCommand =>
        RawArguments is not null
            ? $"{FileName} {RawArguments}"
            : string.Join(' ', new[] { FileName }.Concat(Arguments));
}

/// <summary>
/// A child process plus every descendant it creates. Implementations must
/// only ever act on processes they launched themselves; there is deliberately
/// no API to act on an arbitrary PID or port owner.
/// </summary>
public interface IOwnedProcessGroup : IDisposable
{
    string Label { get; }
    int RootProcessId { get; }
    bool RootHasExited { get; }
    int? RootExitCode { get; }
    Task RootExited { get; }

    /// <summary>Kernel-tracked members (Windows Job Object) or best-effort descendants (POSIX fallback).</summary>
    IReadOnlyList<int> ActiveProcessIds();

    /// <summary>Terminate every member of this group and nothing else.</summary>
    void TerminateAll();

    Task<bool> WaitForAllExitedAsync(TimeSpan timeout, CancellationToken cancellationToken = default);
}

public interface IProcessGroupLauncher
{
    /// <summary>True when closing the group handle (including on controller crash) kills its members.</summary>
    bool ProvidesKillOnClose { get; }

    IOwnedProcessGroup Start(ProcessSpec spec);
}

/// <summary>Abstraction over a Windows Job Object so group logic is testable.</summary>
public interface IJobObject : IDisposable
{
    bool KillOnClose { get; }
    IReadOnlyList<int> ProcessIds();
    void Terminate(uint exitCode);
}

/// <summary>Group backed by a Job Object: membership and termination are kernel-enforced.</summary>
public sealed class JobOwnedProcessGroup : IOwnedProcessGroup
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromMilliseconds(150);
    private readonly IJobObject _job;
    private readonly IRootProcess _root;
    private int _disposed;

    public JobOwnedProcessGroup(string label, IJobObject job, IRootProcess root)
    {
        Label = label;
        _job = job;
        _root = root;
    }

    public string Label { get; }
    public int RootProcessId => _root.Id;
    public bool RootHasExited => _root.HasExited;
    public int? RootExitCode => _root.HasExited ? _root.ExitCode : null;
    public Task RootExited => _root.Exited;

    public IReadOnlyList<int> ActiveProcessIds() =>
        Volatile.Read(ref _disposed) == 1 ? Array.Empty<int>() : _job.ProcessIds();

    public void TerminateAll()
    {
        if (Volatile.Read(ref _disposed) == 0)
        {
            _job.Terminate(1);
        }
    }

    public async Task<bool> WaitForAllExitedAsync(TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            if (ActiveProcessIds().Count == 0)
            {
                return true;
            }
            if (DateTime.UtcNow >= deadline)
            {
                return false;
            }
            await Task.Delay(PollInterval, cancellationToken).ConfigureAwait(false);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1)
        {
            return;
        }
        // Closing the last job handle is itself a kill-on-close backstop.
        _job.Dispose();
        _root.Dispose();
    }
}

public interface IRootProcess : IDisposable
{
    int Id { get; }
    bool HasExited { get; }
    int ExitCode { get; }
    Task Exited { get; }
}

internal sealed class SystemRootProcess : IRootProcess
{
    private readonly System.Diagnostics.Process _process;

    public SystemRootProcess(System.Diagnostics.Process process)
    {
        _process = process;
        Exited = process.WaitForExitAsync();
    }

    public int Id => _process.Id;
    public bool HasExited => _process.HasExited;
    public int ExitCode => _process.ExitCode;
    public Task Exited { get; }
    public void Dispose() => _process.Dispose();
}
