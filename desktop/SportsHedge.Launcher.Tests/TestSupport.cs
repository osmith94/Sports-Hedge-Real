using System.Collections.Concurrent;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Backend;
using SportsHedge.Launcher.Core.Build;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Ports;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Runtime;
using SportsHedge.Launcher.Core.Security;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class WindowsFactAttribute : FactAttribute
{
    public WindowsFactAttribute()
    {
        if (!OperatingSystem.IsWindows())
        {
            Skip = "Requires Windows (Job Objects / Windows named-pipe ACLs); runs on windows-latest CI.";
        }
    }
}

public sealed class E2EFactAttribute : FactAttribute
{
    public const string RepoEnv = "SPORTS_HEDGE_E2E_REPO";

    public E2EFactAttribute(bool windowsOnly = false)
    {
        if (string.IsNullOrWhiteSpace(System.Environment.GetEnvironmentVariable(RepoEnv)))
        {
            Skip = $"Set {RepoEnv} to a prepared Sports Hedge checkout (backend venv + frontend node_modules) to run end-to-end tests.";
        }
        else if (windowsOnly && !OperatingSystem.IsWindows())
        {
            Skip = "Kill-on-close end-to-end test requires Windows Job Objects.";
        }
    }
}

public sealed class AlphabeticalOrderer : Xunit.Sdk.ITestCaseOrderer
{
    public IEnumerable<TTestCase> OrderTestCases<TTestCase>(IEnumerable<TTestCase> testCases)
        where TTestCase : Xunit.Abstractions.ITestCase =>
        testCases.OrderBy(testCase => testCase.TestMethod.Method.Name, StringComparer.Ordinal);
}

public static class TestPaths
{
    public static string DesktopDir
    {
        get
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "SportsHedge.Desktop.sln")))
            {
                dir = dir.Parent;
            }
            return dir?.FullName ?? throw new InvalidOperationException("desktop solution directory not found");
        }
    }

    public static string RepoRoot => Path.GetDirectoryName(DesktopDir)!;

    public static string DotnetHost => System.Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") is { Length: > 0 } host ? host : "dotnet";

    public static string TestHostDll
    {
        get
        {
            // .../SportsHedge.Launcher.Tests/bin/<Configuration>/net8.0/
            var configuration = new DirectoryInfo(AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)).Parent!.Name;
            return Path.Combine(DesktopDir, "SportsHedge.Launcher.TestHost", "bin", configuration, "net8.0", "SportsHedge.Launcher.TestHost.dll");
        }
    }
}

public sealed class RecordingLog : ILog
{
    public ConcurrentQueue<string> Lines { get; } = new();
    public void Info(string message) => Lines.Enqueue("INFO " + message);
    public void Warn(string message) => Lines.Enqueue("WARN " + message);
    public void Error(string message) => Lines.Enqueue("ERROR " + message);
    public bool Contains(string fragment) => Lines.Any(line => line.Contains(fragment, StringComparison.Ordinal));
}

/// <summary>Shared event journal so tests can assert ordering across fakes.</summary>
public sealed class Journal
{
    private readonly ConcurrentQueue<string> _events = new();
    public void Add(string entry) => _events.Enqueue(entry);
    public IReadOnlyList<string> Events => _events.ToArray();
    public int IndexOf(string entry) => Events.ToList().IndexOf(entry);
}

public sealed class FakeGit : IGitReader
{
    public GitIdentity Identity { get; set; } = new("abc1230000000000000000000000000000000000", "main");
    public bool Dirty { get; set; }
    public bool Throw { get; set; }

    public GitIdentity ReadIdentity(string repoRoot) =>
        Throw ? throw new InvalidOperationException("git rev-parse HEAD failed") : Identity;

    public bool IsPathDirty(string repoRoot, string relativePath) => Dirty;
}

public sealed class FakePorts : IPortProbe
{
    public ConcurrentDictionary<int, PortStatus> Occupied { get; } = new();
    public Func<int, bool>? ListeningOverride { get; set; }

    public PortStatus Inspect(int port)
    {
        if (ListeningOverride is not null)
        {
            return new PortStatus(port, ListeningOverride(port));
        }
        return Occupied.TryGetValue(port, out var status) ? status : new PortStatus(port, false);
    }
}

public sealed class FakeHealth : IHealthProbe
{
    public Func<HealthResult> Backend { get; set; } = () => HealthResult.Ok("backend ok");
    public Func<HealthResult> Frontend { get; set; } = () => HealthResult.Ok("frontend ok");

    public Task<HealthResult> CheckBackendAsync(string expectedSha, string sessionId, CancellationToken cancellationToken) =>
        Task.FromResult(Backend());

    public Task<HealthResult> CheckFrontendAsync(string expectedSha, string sessionId, bool includePage, CancellationToken cancellationToken) =>
        Task.FromResult(Frontend());
}

public sealed class FakeJob : IJobObject
{
    private readonly object _gate = new();
    private readonly List<int> _pids;

    public FakeJob(params int[] pids)
    {
        _pids = pids.ToList();
    }

    public int TerminateCalls { get; private set; }
    public bool Disposed { get; private set; }
    public bool KillOnClose => true;

    public IReadOnlyList<int> ProcessIds()
    {
        lock (_gate)
        {
            return _pids.ToArray();
        }
    }

    public void Terminate(uint exitCode)
    {
        lock (_gate)
        {
            TerminateCalls++;
            _pids.Clear();
        }
    }

    public void ExitAll()
    {
        lock (_gate)
        {
            _pids.Clear();
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            Disposed = true;
            _pids.Clear();
        }
    }
}

public sealed class FakeRoot : IRootProcess
{
    private readonly TaskCompletionSource _exited = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public FakeRoot(int id)
    {
        Id = id;
    }

    public int Id { get; }
    public bool HasExited => _exited.Task.IsCompleted;
    public int ExitCode { get; private set; }
    public Task Exited => _exited.Task;

    public void Exit(int code)
    {
        if (_exited.Task.IsCompleted)
        {
            return;
        }
        ExitCode = code;
        _exited.TrySetResult();
    }

    public void Dispose()
    {
    }
}

/// <summary>A fake owned group: behaves like a Job Object group but with in-memory processes.</summary>
public sealed class FakeGroup : IOwnedProcessGroup
{
    private readonly Journal _journal;

    public FakeGroup(string label, int rootPid, Journal journal)
    {
        Label = label;
        Root = new FakeRoot(rootPid);
        Job = new FakeJob(rootPid, rootPid + 1);
        _journal = journal;
        Inner = new JobOwnedProcessGroup(label, Job, Root);
    }

    public FakeRoot Root { get; }
    public FakeJob Job { get; }
    public JobOwnedProcessGroup Inner { get; }
    public string Label { get; }
    public int RootProcessId => Inner.RootProcessId;
    public bool RootHasExited => Inner.RootHasExited;
    public int? RootExitCode => Inner.RootExitCode;
    public Task RootExited => Inner.RootExited;
    public bool Disposed { get; private set; }

    /// <summary>Simulate the process tree exiting on its own (graceful exit or crash).</summary>
    public void ExitNaturally(int code)
    {
        Job.ExitAll();
        Root.Exit(code);
    }

    public IReadOnlyList<int> ActiveProcessIds() => Inner.ActiveProcessIds();

    public void TerminateAll()
    {
        _journal.Add($"{Label}:terminate");
        Inner.TerminateAll();
        Root.Exit(1);
    }

    public Task<bool> WaitForAllExitedAsync(TimeSpan timeout, CancellationToken cancellationToken = default) =>
        Inner.WaitForAllExitedAsync(timeout, cancellationToken);

    public void Dispose()
    {
        _journal.Add($"{Label}:dispose");
        Disposed = true;
        Inner.Dispose();
    }
}

public sealed class FakeLauncher : IProcessGroupLauncher
{
    private readonly Journal _journal;
    private int _nextPid = 1000;

    public FakeLauncher(Journal journal)
    {
        _journal = journal;
    }

    public bool ProvidesKillOnClose => true;
    public List<ProcessSpec> Started { get; } = new();
    public Dictionary<string, FakeGroup> Groups { get; } = new();

    public IOwnedProcessGroup Start(ProcessSpec spec)
    {
        _journal.Add($"start:{spec.Label}");
        Started.Add(spec);
        var group = new FakeGroup(spec.Label, Interlocked.Add(ref _nextPid, 10), _journal);
        Groups[spec.Label] = group;
        return group;
    }
}

public sealed class FakeBuilder : IFrontendBuilder
{
    private readonly Journal _journal;
    private readonly RuntimeLayout _layout;

    public FakeBuilder(Journal journal, RuntimeLayout layout)
    {
        _journal = journal;
        _layout = layout;
    }

    public bool Fail { get; set; }
    public bool FailDependencies { get; set; }
    public int Builds { get; private set; }
    public TaskCompletionSource? Gate { get; set; }

    public Task<FrontendBuildResult> EnsureDependenciesAsync(GitIdentity git, CancellationToken cancellationToken)
    {
        _journal.Add("dependencies");
        return Task.FromResult(FailDependencies
            ? new FrontendBuildResult(false, "npm ci failed with exit code 1.")
            : new FrontendBuildResult(true, "present"));
    }

    public async Task<FrontendBuildResult> BuildAsync(GitIdentity git, bool frontendDirty, CancellationToken cancellationToken)
    {
        _journal.Add("build");
        Builds++;
        FrontendBuildMarkerStore.Delete(_layout.FrontendBuildMarkerPath);
        if (Gate is not null)
        {
            await Gate.Task.WaitAsync(cancellationToken);
        }
        if (Fail)
        {
            return new FrontendBuildResult(false, "npm run build failed with exit code 1.");
        }
        TestLayouts.WriteBuild(_layout, git.Sha, frontendDirty);
        return new FrontendBuildResult(true, "built");
    }
}

public sealed class FakeBackendShutdown : IBackendShutdownClient
{
    private readonly Journal _journal;

    public FakeBackendShutdown(Journal journal)
    {
        _journal = journal;
    }

    public Action? OnRequest { get; set; }
    public bool Accept { get; set; } = true;
    public int Calls { get; private set; }
    public List<string> Tokens { get; } = new();

    public Task<BackendShutdownResult> RequestShutdownAsync(string token, CancellationToken cancellationToken)
    {
        _journal.Add("backend:graceful-request");
        Calls++;
        Tokens.Add(token);
        OnRequest?.Invoke();
        return Task.FromResult(new BackendShutdownResult(Accept, Accept ? "accepted" : "refused"));
    }
}

public sealed class FakeIpc : IControllerIpcHost
{
    public Func<string, string>? Handler { get; private set; }
    public bool Stopped { get; private set; }
    public void Start(Func<string, string> handler) => Handler = handler;

    public ValueTask StopAsync()
    {
        Stopped = true;
        return ValueTask.CompletedTask;
    }
}

public sealed class FakePrereqs : IPrerequisites
{
    public string? Problem { get; set; }
    public string? Check(RuntimeLayout layout) => Problem;
}

public static class TestLayouts
{
    public static RuntimeLayout Create()
    {
        var root = Path.Combine(Path.GetTempPath(), "sh-launcher-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "frontend", ".next"));
        Directory.CreateDirectory(Path.Combine(root, "backend"));
        Directory.CreateDirectory(Path.Combine(root, "logs"));
        return new RuntimeLayout(root, OperatingSystem.IsWindows());
    }

    public static void WriteBuild(RuntimeLayout layout, string sha, bool dirty = false, string buildId = "build-1")
    {
        Directory.CreateDirectory(layout.NextDir);
        File.WriteAllText(layout.NextBuildIdPath, buildId);
        FrontendBuildMarkerStore.Write(layout.FrontendBuildMarkerPath, new FrontendBuildMarker
        {
            GitSha = sha,
            GitBranch = "main",
            BuildTimestamp = "2026-09-25T00:00:00Z",
            NextBuildId = buildId,
            FrontendDirty = dirty,
            Builder = "test",
        });
    }
}

/// <summary>A controller wired entirely to fakes with fast timeouts.</summary>
public sealed class Harness
{
    public Harness(bool markerMatches = true)
    {
        Layout = TestLayouts.Create();
        Secrets = SessionSecrets.Create();
        Log = new RecordingLog();
        Journal = new Journal();
        Git = new FakeGit();
        Ports = new FakePorts();
        Health = new FakeHealth();
        Launcher = new FakeLauncher(Journal);
        Builder = new FakeBuilder(Journal, Layout);
        BackendShutdown = new FakeBackendShutdown(Journal);
        Ipc = new FakeIpc();
        Prereqs = new FakePrereqs();
        if (markerMatches)
        {
            TestLayouts.WriteBuild(Layout, Git.Identity.Sha);
        }
        // Default graceful behaviour: the backend tree exits once asked.
        BackendShutdown.OnRequest = () => Launcher.Groups.GetValueOrDefault("backend")?.ExitNaturally(0);
        var specs = new ServiceSpecFactory(Layout, Secrets, new Dictionary<string, string>(), "npm");
        Controller = new SessionController(Layout, Secrets,
            new SessionDependencies(Log, Git, Ports, Health, Launcher, Builder, BackendShutdown, Prereqs, specs, Ipc),
            Options);
    }

    public static SessionOptions Options { get; } = new()
    {
        BackendHealthTimeout = TimeSpan.FromMilliseconds(600),
        FrontendHealthTimeout = TimeSpan.FromMilliseconds(600),
        HealthPollInterval = TimeSpan.FromMilliseconds(20),
        BackendGracefulTimeout = TimeSpan.FromMilliseconds(400),
        ForcedExitTimeout = TimeSpan.FromMilliseconds(400),
        FrontendStopTimeout = TimeSpan.FromMilliseconds(400),
        PortReleaseTimeout = TimeSpan.FromMilliseconds(300),
        MonitorInterval = TimeSpan.FromMilliseconds(30),
        MonitorHealthEveryTicks = 2,
        HealthFailuresBeforeDegraded = 2,
    };

    public RuntimeLayout Layout { get; }
    public SessionSecrets Secrets { get; }
    public RecordingLog Log { get; }
    public Journal Journal { get; }
    public FakeGit Git { get; }
    public FakePorts Ports { get; }
    public FakeHealth Health { get; }
    public FakeLauncher Launcher { get; }
    public FakeBuilder Builder { get; }
    public FakeBackendShutdown BackendShutdown { get; }
    public FakeIpc Ipc { get; }
    public FakePrereqs Prereqs { get; }
    public SessionController Controller { get; }

    public FakeGroup Backend => Launcher.Groups["backend"];
    public FakeGroup Frontend => Launcher.Groups["frontend"];

    public string IpcRequest(string type, string? token, string source = "ui") =>
        Ipc.Handler!(System.Text.Json.JsonSerializer.Serialize(new { type, token, source }));
}
