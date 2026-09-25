using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.SingleInstance;

namespace SportsHedge.Launcher;

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        ApplicationConfiguration.Initialize();

        using var guard = SingleInstanceGuard.Acquire(LauncherConstants.SingleInstanceMutexName);
        if (!guard.IsPrimary)
        {
            // Never start a second backend/frontend/controller.
            SecondaryInstance.Handle(new WindowsInstanceActivation(), BrowserLauncher.Open);
            return 0;
        }

        var location = RepoLocator.Locate(AppContext.BaseDirectory, Environment.GetEnvironmentVariable(LauncherConstants.RepoRootOverrideEnv));
        if (!location.Found)
        {
            MessageBox.Show(
                $"Sports Hedge could not start.\n\nFailed component: Repository\n\nReason:\n{location.Detail}",
                "Sports Hedge", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        var layout = RuntimeLayout.ForCurrentPlatform(location.RepoRoot!);
        var redactor = new SecretRedactor();
        var secrets = SessionSecrets.Create();
        var log = new FileControllerLog(layout.ControllerLog, redactor);
        log.Info($"==== SportsHedge.exe {typeof(Program).Assembly.GetName().Version} starting; repo located via {location.Detail}");

        var controller = SessionFactory.Create(layout, secrets, redactor, log);
        using var activation = new WindowsInstanceActivation();
        using var context = new TrayApplicationContext(controller, activation, log);

        Application.ThreadException += (_, e) => context.OnUnhandledException(e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            log.Error($"unhandled_exception {e.ExceptionObject}; Job Objects kill owned children if the controller exits");

        Application.Run(context);
        log.Info($"==== SportsHedge.exe exiting code={context.ExitCode}");
        return context.ExitCode;
    }
}
