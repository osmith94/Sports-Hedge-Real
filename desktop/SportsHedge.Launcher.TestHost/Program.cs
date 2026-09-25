using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Processes.Windows;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.SingleInstance;
using SportsHedge.Launcher.Core.State;

// Headless harness around the same Core used by SportsHedge.exe.
//   job-probe                       start a dummy child tree in a kill-on-close Job Object, print its PIDs, wait forever
//   session --repo <root> [--mutex] run a full controller session; stdin "tray-exit" simulates tray Exit
return args.Length > 0 && args[0] == "job-probe"
    ? RunJobProbe()
    : await RunSessionAsync(args);

static int RunJobProbe()
{
    if (!OperatingSystem.IsWindows())
    {
        Console.Error.WriteLine("job-probe requires Windows");
        return 2;
    }
    var logs = Path.Combine(Path.GetTempPath(), "sports-hedge-job-probe");
    var cmd = Environment.GetEnvironmentVariable("ComSpec") ?? @"C:\Windows\System32\cmd.exe";
    var spec = new ProcessSpec(
        "probe",
        cmd,
        Array.Empty<string>(),
        Path.GetTempPath(),
        SportsHedge.Launcher.Core.ProcessEnvironment.ChildEnvironment.CurrentProcess(),
        Path.Combine(logs, "probe.out.log"),
        Path.Combine(logs, "probe.err.log"),
        "/d /c ping -n 300 127.0.0.1");
    var group = new WindowsJobProcessLauncher().Start(spec);
    for (var i = 0; i < 50 && group.ActiveProcessIds().Count < 2; i++)
    {
        Thread.Sleep(100);
    }
    Console.WriteLine($"JOB_PIDS={string.Join(',', group.ActiveProcessIds())}");
    Console.Out.Flush();
    Thread.Sleep(Timeout.Infinite);
    GC.KeepAlive(group);
    return 0;
}

static async Task<int> RunSessionAsync(string[] args)
{
    string? repo = null;
    var mutexName = LauncherConstants.SingleInstanceMutexName;
    for (var i = 0; i < args.Length; i++)
    {
        if (args[i] == "--repo" && i + 1 < args.Length) repo = args[++i];
        else if (args[i] == "--mutex" && i + 1 < args.Length) mutexName = args[++i];
    }
    if (repo is null || !RepoLocator.IsSportsHedgeRepo(repo))
    {
        Console.Error.WriteLine("usage: session --repo <sports-hedge checkout> [--mutex <name>]");
        return 2;
    }

    using var guard = SingleInstanceGuard.Acquire(mutexName);
    if (!guard.IsPrimary)
    {
        Console.WriteLine("SECONDARY_INSTANCE no children started");
        return 0;
    }

    var layout = RuntimeLayout.ForCurrentPlatform(repo);
    var redactor = new SecretRedactor();
    var secrets = SessionSecrets.Create();
    var log = new FileControllerLog(layout.ControllerLog, redactor, line => Console.WriteLine($"LOG {line}"));
    var controller = SessionFactory.Create(layout, secrets, redactor, log);
    controller.State.Changed += status => Console.WriteLine($"STATE={status.State} MESSAGE={status.Message}");

    var stdin = new Thread(() =>
    {
        string? line;
        while ((line = Console.ReadLine()) is not null)
        {
            if (line.Trim() == "tray-exit") controller.RequestShutdown(ShutdownSource.Tray);
            if (line.Trim() == "controller-exit") controller.RequestShutdown(ShutdownSource.ControllerExit);
        }
    }) { IsBackground = true };
    stdin.Start();

    var result = await controller.StartAsync();
    if (result.Success)
    {
        Console.WriteLine($"RUNNING BACKEND_PIDS={string.Join(',', controller.BackendProcessIds)} " +
                          $"FRONTEND_PIDS={string.Join(',', controller.FrontendProcessIds)} " +
                          $"PIPE_PATH_SET={!string.IsNullOrEmpty(secrets.PipePath)}");
    }
    else
    {
        Console.WriteLine($"STARTUP_FAILED component={result.FailedComponent} reason={result.Reason?.Replace('\n', ' ')}");
    }
    var report = await controller.Completion;
    Console.WriteLine($"STOPPED source={report.Source} failed={report.Failed} backend_graceful={report.BackendGraceful} " +
                      $"backend_forced={report.BackendForced} remaining=[{string.Join(',', report.RemainingProcessIds)}] " +
                      $"ports=[{string.Join(',', report.PortsStillListening)}]");
    if (!result.Success && !result.StoppedByRequest) return 3;
    return report.RemainingProcessIds.Count == 0 && report.PortsStillListening.Count == 0 ? 0 : 4;
}
