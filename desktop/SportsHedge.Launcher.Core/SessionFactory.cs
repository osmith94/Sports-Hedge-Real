using SportsHedge.Launcher.Core.Backend;
using SportsHedge.Launcher.Core.Build;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Ports;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Processes.Windows;
using SportsHedge.Launcher.Core.ProcessEnvironment;
using SportsHedge.Launcher.Core.Runtime;
using SportsHedge.Launcher.Core.Security;

namespace SportsHedge.Launcher.Core;

public static class SessionFactory
{
    public static IProcessGroupLauncher CreatePlatformLauncher() =>
        OperatingSystem.IsWindows() ? new WindowsJobProcessLauncher() : new PosixProcessGroupLauncher();

    /// <summary>Real dependencies. Secrets are registered with the log redactor before anything is logged.</summary>
    public static SessionController Create(
        RuntimeLayout layout,
        SessionSecrets secrets,
        SecretRedactor redactor,
        ILog log,
        SessionOptions? options = null)
    {
        redactor.Register(secrets.ControllerToken);
        redactor.Register(secrets.BackendShutdownToken);

        var npm = ToolLocator.FindOnPath(ToolLocator.NpmFileName);
        var node = ToolLocator.FindOnPath(ToolLocator.NodeFileName);
        var git = ToolLocator.FindOnPath(ToolLocator.GitFileName);
        var launcher = CreatePlatformLauncher();
        var specs = new ServiceSpecFactory(layout, secrets, ChildEnvironment.CurrentProcess(), npm ?? ToolLocator.NpmFileName);
        var deps = new SessionDependencies(
            log,
            new GitCli(git ?? ToolLocator.GitFileName),
            new SystemPortProbe(),
            new HttpHealthProbe(),
            launcher,
            new NpmFrontendBuilder(layout, launcher, specs, log),
            new HttpBackendShutdownClient(),
            new SystemPrerequisites(npm, node, git),
            specs,
            new PipeControllerIpcHost(secrets.PipeName, log));
        return new SessionController(layout, secrets, deps, options);
    }
}
