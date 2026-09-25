using SportsHedge.Launcher.Core.State;

namespace SportsHedge.Launcher.Core;

public static class OperatorMessages
{
    public const string ExitConfirmTitle = "Exit Sports Hedge?";
    public const string ExitConfirmBody = "This will stop the scanner, backend and local interface.";

    public static string TrayText(ControllerState state) => state switch
    {
        ControllerState.NotStarted or ControllerState.Starting => LauncherConstants.TrayStarting,
        ControllerState.Running => LauncherConstants.TrayRunning,
        ControllerState.Degraded => LauncherConstants.TrayDegraded,
        ControllerState.Stopping => LauncherConstants.TrayStopping,
        ControllerState.Stopped => LauncherConstants.TrayStopped,
        ControllerState.Failed => LauncherConstants.TrayFailed,
        _ => LauncherConstants.TrayStarting,
    };

    public static bool CanOpenBrowser(ControllerState state) =>
        state is ControllerState.Running or ControllerState.Degraded;

    public static string StartupFailure(StartupResult result, string logsDir)
    {
        var git = result.Git is null ? "(not read)" : $"{result.Git.Sha} ({result.Git.Branch})";
        return
            $"Sports Hedge could not start.\n\n" +
            $"Failed component: {result.FailedComponent ?? "Controller"}\n\n" +
            $"Reason:\n{result.Reason}\n\n" +
            $"Logs: {logsDir}\n" +
            $"Git SHA / branch: {git}\n\n" +
            "Any Sports Hedge processes this launcher started have been stopped. No other processes were terminated.";
    }
}
