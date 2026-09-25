namespace SportsHedge.Launcher.Core;

public static class LauncherConstants
{
    public const string LoopbackHost = "127.0.0.1";
    public const int BackendPort = 8000;
    public const int FrontendPort = 3000;
    public const string AppUrl = "http://127.0.0.1:3000/";
    public const string BackendBaseUrl = "http://127.0.0.1:8000";
    public const string FrontendBaseUrl = "http://127.0.0.1:3000";
    public const string DesktopOrigins = "http://127.0.0.1:3000,http://localhost:3000";

    // Local\ = per logon session. A second Windows user would hit the port
    // preflight and be refused rather than share this controller.
    public const string SingleInstanceMutexName = @"Local\SportsHedge.DesktopController.v1";
    public const string ActivationEventName = @"Local\SportsHedge.DesktopController.v1.Activate";

    public const string PipeNamePrefix = "sports-hedge-controller-";
    public const string LauncherConfigFileName = "SportsHedge.launcher.json";
    public const string RepoRootOverrideEnv = "SPORTS_HEDGE_REPO_ROOT";

    public const string TrayStarting = "Sports Hedge — Starting";
    public const string TrayRunning = "Sports Hedge — PAPER MODE — Running";
    public const string TrayDegraded = "Sports Hedge — Degraded";
    public const string TrayStopping = "Sports Hedge — Stopping";
    public const string TrayStopped = "Sports Hedge — Stopped";
    public const string TrayFailed = "Sports Hedge — Startup failed";
}
