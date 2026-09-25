using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Ipc;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.ProcessEnvironment;
using SportsHedge.Launcher.Core.Security;

namespace SportsHedge.Launcher.Core.Runtime;

public static class ToolLocator
{
    /// <summary>Finds an executable on PATH (Windows: honours .cmd/.exe names given explicitly).</summary>
    public static string? FindOnPath(string fileName, string? pathValue = null)
    {
        var path = pathValue ?? System.Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var dir in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            try
            {
                var candidate = Path.Combine(dir.Trim('"'), fileName);
                if (File.Exists(candidate))
                {
                    return Path.GetFullPath(candidate);
                }
            }
            catch (ArgumentException)
            {
            }
        }
        return null;
    }

    public static string NpmFileName => OperatingSystem.IsWindows() ? "npm.cmd" : "npm";
    public static string NodeFileName => OperatingSystem.IsWindows() ? "node.exe" : "node";
    public static string GitFileName => OperatingSystem.IsWindows() ? "git.exe" : "git";

    public static string CommandProcessor =>
        System.Environment.GetEnvironmentVariable("ComSpec")
        ?? Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.System), "cmd.exe");
}

public interface IPrerequisites
{
    /// <summary>Null when everything needed to start is present; otherwise an operator-facing explanation.</summary>
    string? Check(RuntimeLayout layout);
}

public sealed class SystemPrerequisites : IPrerequisites
{
    private readonly string? _npm;
    private readonly string? _node;
    private readonly string? _git;

    public SystemPrerequisites(string? npm, string? node, string? git)
    {
        _npm = npm;
        _node = node;
        _git = git;
    }

    public string? Check(RuntimeLayout layout)
    {
        if (_git is null)
        {
            return "git was not found on PATH. Sports Hedge records the serving Git SHA and needs git to read the local checkout.";
        }
        if (!File.Exists(layout.PythonExecutable))
        {
            return $"Python venv not found at {layout.PythonExecutable}. Create it with:\n" +
                   "cd backend\npython -m venv .venv\n.venv\\Scripts\\python -m pip install -e \".[dev]\"";
        }
        if (_node is null || _npm is null)
        {
            return "Node.js/npm was not found on PATH. Install Node.js 22, then try again.";
        }
        if (!File.Exists(Path.Combine(layout.FrontendDir, "package.json")))
        {
            return $"frontend\\package.json is missing under {layout.RepoRoot}.";
        }
        return null;
    }
}

public interface IServiceSpecFactory
{
    ProcessSpec Backend(GitIdentity git);
    ProcessSpec Frontend(GitIdentity git);
    ProcessSpec FrontendBuild(GitIdentity git);
    ProcessSpec FrontendInstall(GitIdentity git);
}

/// <summary>
/// Backend: python -m sports_hedge.api.desktop_host --host 127.0.0.1 --port 8000.
/// Frontend: production Next.js via `npm run start -- -H 127.0.0.1 -p 3000` (never `next dev`).
/// </summary>
public sealed class ServiceSpecFactory : IServiceSpecFactory
{
    private readonly RuntimeLayout _layout;
    private readonly SessionSecrets _secrets;
    private readonly IReadOnlyDictionary<string, string> _parentEnvironment;
    private readonly string _npm;

    public ServiceSpecFactory(RuntimeLayout layout, SessionSecrets secrets, IReadOnlyDictionary<string, string> parentEnvironment, string npm)
    {
        _layout = layout;
        _secrets = secrets;
        _parentEnvironment = parentEnvironment;
        _npm = npm;
    }

    public ProcessSpec Backend(GitIdentity git) => new(
        "backend",
        _layout.PythonExecutable,
        new[]
        {
            "-m", "sports_hedge.api.desktop_host",
            "--host", LauncherConstants.LoopbackHost,
            "--port", LauncherConstants.BackendPort.ToString(System.Globalization.CultureInfo.InvariantCulture),
        },
        _layout.BackendDir,
        ChildEnvironment.Build(ChildRole.Backend, _parentEnvironment, _secrets, git, _layout),
        _layout.BackendOutLog,
        _layout.BackendErrLog);

    public ProcessSpec Frontend(GitIdentity git) => Npm(
        "frontend",
        new[] { "run", "start", "--", "-H", LauncherConstants.LoopbackHost, "-p", LauncherConstants.FrontendPort.ToString(System.Globalization.CultureInfo.InvariantCulture) },
        ChildEnvironment.Build(ChildRole.Frontend, _parentEnvironment, _secrets, git, _layout),
        _layout.FrontendOutLog,
        _layout.FrontendErrLog);

    public ProcessSpec FrontendBuild(GitIdentity git) => Npm(
        "frontend-build",
        new[] { "run", "build" },
        ChildEnvironment.Build(ChildRole.FrontendBuild, _parentEnvironment, _secrets, git, _layout),
        _layout.FrontendBuildOutLog,
        _layout.FrontendBuildErrLog);

    // `npm ci` never rewrites package-lock.json, so the checkout is not mutated.
    public ProcessSpec FrontendInstall(GitIdentity git) => Npm(
        "frontend-install",
        new[] { "ci", "--include=dev", "--no-audit", "--no-fund" },
        ChildEnvironment.Build(ChildRole.FrontendInstall, _parentEnvironment, _secrets, git, _layout),
        _layout.FrontendBuildOutLog,
        _layout.FrontendBuildErrLog);

    private ProcessSpec Npm(string label, string[] args, IReadOnlyDictionary<string, string> env, string stdout, string stderr)
    {
        if (!_layout.IsWindows)
        {
            return new ProcessSpec(label, _npm, args, _layout.FrontendDir, env, stdout, stderr);
        }
        // npm.cmd needs cmd.exe. /s strips only the outer quotes of the /c string.
        var raw = $"/d /s /c \"\"{_npm}\" {string.Join(' ', args)}\"";
        return new ProcessSpec(label, ToolLocator.CommandProcessor, Array.Empty<string>(), _layout.FrontendDir, env, stdout, stderr, raw);
    }
}

public interface IControllerIpcHost
{
    void Start(Func<string, string> handler);
    ValueTask StopAsync();
}

public sealed class PipeControllerIpcHost : IControllerIpcHost
{
    private readonly string _pipeName;
    private readonly ILog _log;
    private ControllerPipeServer? _server;

    public PipeControllerIpcHost(string pipeName, ILog log)
    {
        _pipeName = pipeName;
        _log = log;
    }

    public void Start(Func<string, string> handler)
    {
        _server = new ControllerPipeServer(_pipeName, handler, _log);
        _server.Start();
    }

    public async ValueTask StopAsync()
    {
        if (_server is not null)
        {
            await _server.DisposeAsync().ConfigureAwait(false);
            _server = null;
        }
    }
}
