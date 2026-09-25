using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Build;
using SportsHedge.Launcher.Core.Ports;
using Xunit;
using Xunit.Abstractions;

namespace SportsHedge.Launcher.Tests;

/// <summary>
/// Real backend (desktop_host) + production Next.js + controller, driven
/// through the headless test host that uses the same Core as SportsHedge.exe.
/// All scenarios share ports 8000/3000, so they live in one (serial) class,
/// run in name order (A rebuilds; B then proves a matching build is reused).
/// </summary>
[TestCaseOrderer("SportsHedge.Launcher.Tests.AlphabeticalOrderer", "SportsHedge.Launcher.Tests")]
public sealed class EndToEndTests
{
    private static readonly TimeSpan StartupTimeout = TimeSpan.FromMinutes(10);
    private static readonly TimeSpan StopTimeout = TimeSpan.FromSeconds(90);
    private readonly ITestOutputHelper _output;

    public EndToEndTests(ITestOutputHelper output)
    {
        _output = output;
    }

    private static string Repo => System.Environment.GetEnvironmentVariable(E2EFactAttribute.RepoEnv)!;
    private static RuntimeLayout Layout => RuntimeLayout.ForCurrentPlatform(Repo);

    [E2EFact]
    public async Task A_ui_exit_after_sha_rebuild_stops_everything_and_runs_lifespan_cleanup()
    {
        // Scenario I: stale marker -> rebuild. Scenario A: UI exit.
        if (File.Exists(Layout.NextBuildIdPath))
        {
            TestLayouts.WriteBuild(Layout, "0000000000000000000000000000000000000000",
                buildId: FrontendBuildMarkerStore.ReadNextBuildId(Layout.NextBuildIdPath)!);
        }
        await using var host = HostRun.Start(Repo, NewMutex(), _output);
        var running = await host.WaitForLineAsync("RUNNING", StartupTimeout);
        Assert.True(host.Saw("frontend_install_result success=True") || host.Saw("install=False reason=Match"),
            "frontend dependency freshness was not established");
        Assert.True(host.Saw("rebuild=True"), "stale build SHA did not trigger a rebuild");
        Assert.True(host.Saw("frontend_rebuild_result success=True"));
        var pids = HostRun.ParsePids(running);
        AssertPortsListening(true);

        using var http = NewClient();
        Assert.Equal(HttpStatusCode.Forbidden, (await PostExit(http, origin: "https://evil.example")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await PostExit(http, customHeader: false)).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await http.PostAsync("http://127.0.0.1:8000/desktop/shutdown", null)).StatusCode);
        using (var wrong = new HttpRequestMessage(HttpMethod.Post, "http://127.0.0.1:8000/desktop/shutdown"))
        {
            wrong.Headers.Authorization = new AuthenticationHeaderValue("Bearer", new string('A', 43));
            Assert.Equal(HttpStatusCode.Forbidden, (await http.SendAsync(wrong)).StatusCode);
        }
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync("http://127.0.0.1:8000/health")).StatusCode);

        var exit = await PostExit(http);
        Assert.Equal(HttpStatusCode.Accepted, exit.StatusCode);
        Assert.Contains("\"stopping\"", await exit.Content.ReadAsStringAsync());

        var stopped = await host.WaitForLineAsync("STOPPED", StopTimeout);
        Assert.Contains("source=Ui", stopped);
        Assert.Contains("backend_graceful=True", stopped);
        Assert.Contains("remaining=[]", stopped);
        Assert.Contains("ports=[]", stopped);
        Assert.Contains("verification=Verified", stopped);
        Assert.Contains("clean=True", stopped);
        Assert.Equal(0, await host.WaitForExitAsync(StopTimeout));
        await AssertAllGone(pids);
        AssertPortsListening(false);

        var backendErr = File.ReadAllText(Layout.BackendErrLog);
        Assert.Contains("desktop_shutdown_accepted", backendErr);
        Assert.Contains("Application shutdown complete.", backendErr);
        Assert.Contains("desktop_host_stopped", backendErr);
    }

    [E2EFact]
    public async Task B_matching_build_starts_fast_second_instance_exits_and_tray_exit_stops()
    {
        var mutex = NewMutex();
        await using var host = HostRun.Start(Repo, mutex, _output);
        var running = await host.WaitForLineAsync("RUNNING", StartupTimeout);
        Assert.True(host.Saw("rebuild=False reason=Match"), "matching build SHA was rebuilt");
        Assert.True(host.Saw("install=False reason=Match"), "matching package-lock fingerprint re-ran npm ci");
        Assert.False(host.Saw("frontend_install_start"));
        var pids = HostRun.ParsePids(running);

        // Scenario G: a second launcher never starts duplicate children.
        await using (var second = HostRun.Start(Repo, mutex, _output))
        {
            await second.WaitForLineAsync("SECONDARY_INSTANCE", TimeSpan.FromSeconds(60));
            Assert.Equal(0, await second.WaitForExitAsync(TimeSpan.FromSeconds(30)));
        }
        Assert.Equal(pids.OrderBy(p => p), HostRun.ParsePids(running).OrderBy(p => p));
        using var http = NewClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync("http://127.0.0.1:3000/api/desktop/status")).StatusCode);

        // Scenario B: tray exit uses the same shutdown state machine.
        await host.SendAsync("tray-exit");
        var stopped = await host.WaitForLineAsync("STOPPED", StopTimeout);
        Assert.Contains("source=Tray", stopped);
        Assert.Contains("backend_graceful=True", stopped);
        Assert.Contains("verification=Verified", stopped);
        Assert.Equal(0, await host.WaitForExitAsync(StopTimeout));
        await AssertAllGone(pids);
        AssertPortsListening(false);
    }

    [E2EFact]
    public async Task C_backend_crash_is_detected_as_degraded_then_exit_cleans_up()
    {
        await using var host = HostRun.Start(Repo, NewMutex(), _output);
        var running = await host.WaitForLineAsync("RUNNING", StartupTimeout);
        var backend = HostRun.ParsePids(running, "BACKEND_PIDS=");

        foreach (var pid in backend)
        {
            TryKill(pid); // external crash simulation
        }
        var degraded = await host.WaitForLineAsync("STATE=Degraded", TimeSpan.FromSeconds(30));
        Assert.Contains("Backend process exited unexpectedly", degraded);

        await host.SendAsync("tray-exit");
        var stopped = await host.WaitForLineAsync("STOPPED", StopTimeout);
        Assert.Contains("remaining=[]", stopped);
        Assert.Contains("verification=Verified", stopped);
        Assert.Equal(0, await host.WaitForExitAsync(StopTimeout));
        AssertPortsListening(false);
    }

    [E2EFact]
    public async Task D_frontend_crash_is_detected_as_degraded()
    {
        await using var host = HostRun.Start(Repo, NewMutex(), _output);
        var running = await host.WaitForLineAsync("RUNNING", StartupTimeout);
        var frontend = HostRun.ParsePids(running, "FRONTEND_PIDS=");

        foreach (var pid in frontend)
        {
            TryKill(pid);
        }
        var degraded = await host.WaitForLineAsync("STATE=Degraded", TimeSpan.FromSeconds(30));
        Assert.Contains("Interface process exited unexpectedly", degraded);

        await host.SendAsync("tray-exit");
        var stopped = await host.WaitForLineAsync("STOPPED", StopTimeout);
        Assert.Contains("verification=Verified", stopped);
        Assert.Equal(0, await host.WaitForExitAsync(StopTimeout));
        AssertPortsListening(false);
    }

    [E2EFact]
    public async Task E_unrelated_port_occupant_refuses_startup_and_is_untouched()
    {
        using var listener = new TcpListener(IPAddress.Loopback, LauncherConstants.BackendPort);
        listener.Start();

        await using var host = HostRun.Start(Repo, NewMutex(), _output);
        var failed = await host.WaitForLineAsync("STARTUP_FAILED", TimeSpan.FromSeconds(120));
        Assert.Contains("component=Ports", failed);
        Assert.Contains("No processes were terminated", failed);
        var stopped = await host.WaitForLineAsync("STOPPED", StopTimeout);
        Assert.Contains("remaining=[]", stopped);
        Assert.Contains("verification=ForeignPortOccupant", stopped);
        Assert.Contains("clean=False", stopped);
        Assert.Equal(3, await host.WaitForExitAsync(StopTimeout));
        Assert.False(host.Saw("backend_started"));

        using var client = new TcpClient();
        await client.ConnectAsync(IPAddress.Loopback, LauncherConstants.BackendPort);
        Assert.True(client.Connected, "unrelated listener was disturbed");
    }

    [E2EFact(windowsOnly: true)]
    public async Task F_killing_the_controller_kills_backend_and_frontend_via_job_objects()
    {
        // Scenario C: Task Manager "End task" == TerminateProcess on the controller.
        await using var host = HostRun.Start(Repo, NewMutex(), _output);
        var running = await host.WaitForLineAsync("RUNNING", StartupTimeout);
        var pids = HostRun.ParsePids(running);
        Assert.NotEmpty(pids);

        host.KillHard();

        await AssertAllGone(pids);
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
        while (DateTime.UtcNow < deadline && (IsListening(LauncherConstants.BackendPort) || IsListening(LauncherConstants.FrontendPort)))
        {
            await Task.Delay(200);
        }
        AssertPortsListening(false);
    }

    private static string NewMutex() => @"Local\SportsHedge.E2E." + Guid.NewGuid().ToString("N");

    private static HttpClient NewClient() =>
        new(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(10) };

    private static Task<HttpResponseMessage> PostExit(HttpClient http, string origin = "http://127.0.0.1:3000", bool customHeader = true)
    {
        var request = new HttpRequestMessage(HttpMethod.Post, "http://127.0.0.1:3000/api/desktop/exit")
        {
            Content = new StringContent("{\"confirm\":true}", Encoding.UTF8, "application/json"),
        };
        request.Headers.Add("Origin", origin);
        request.Headers.Add("Sec-Fetch-Site", origin == "http://127.0.0.1:3000" ? "same-origin" : "cross-site");
        if (customHeader)
        {
            request.Headers.Add("x-sports-hedge-action", "exit");
        }
        return http.SendAsync(request);
    }

    private static bool IsListening(int port) => new SystemPortProbe().Inspect(port).Listening;

    private static void AssertPortsListening(bool expected)
    {
        Assert.Equal(expected, IsListening(LauncherConstants.BackendPort));
        Assert.Equal(expected, IsListening(LauncherConstants.FrontendPort));
    }

    private static void TryKill(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            process.Kill();
        }
        catch (ArgumentException)
        {
        }
        catch (InvalidOperationException)
        {
        }
    }

    private static async Task AssertAllGone(IEnumerable<int> pids)
    {
        var list = pids.ToList();
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
        while (DateTime.UtcNow < deadline)
        {
            if (list.All(pid => !Alive(pid)))
            {
                return;
            }
            await Task.Delay(200);
        }
        Assert.Fail($"owned processes still alive: {string.Join(',', list.Where(Alive))}");
    }

    private static bool Alive(int pid)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            return !p.HasExited;
        }
        catch (ArgumentException)
        {
            return false;
        }
    }

    private sealed class HostRun : IAsyncDisposable
    {
        private readonly Process _process;
        private readonly ConcurrentQueue<string> _lines = new();
        private readonly ITestOutputHelper _output;

        private HostRun(Process process, ITestOutputHelper output)
        {
            _process = process;
            _output = output;
            _process.OutputDataReceived += (_, e) =>
            {
                if (e.Data is null) return;
                _lines.Enqueue(e.Data);
                try { _output.WriteLine(e.Data); } catch (InvalidOperationException) { }
            };
            _process.ErrorDataReceived += (_, e) =>
            {
                if (e.Data is null) return;
                _lines.Enqueue("STDERR " + e.Data);
                try { _output.WriteLine("STDERR " + e.Data); } catch (InvalidOperationException) { }
            };
            _process.BeginOutputReadLine();
            _process.BeginErrorReadLine();
        }

        public static HostRun Start(string repo, string mutex, ITestOutputHelper output)
        {
            var psi = new ProcessStartInfo(TestPaths.DotnetHost)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var arg in new[] { TestPaths.TestHostDll, "session", "--repo", repo, "--mutex", mutex })
            {
                psi.ArgumentList.Add(arg);
            }
            return new HostRun(Process.Start(psi)!, output);
        }

        public bool Saw(string fragment) => _lines.Any(line => line.Contains(fragment, StringComparison.Ordinal));

        public async Task<string> WaitForLineAsync(string prefix, TimeSpan timeout)
        {
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                var match = _lines.FirstOrDefault(line => line.StartsWith(prefix, StringComparison.Ordinal));
                if (match is not null)
                {
                    return match;
                }
                if (_process.HasExited && !_lines.Any(line => line.StartsWith(prefix, StringComparison.Ordinal)))
                {
                    await Task.Delay(500);
                    match = _lines.FirstOrDefault(line => line.StartsWith(prefix, StringComparison.Ordinal));
                    return match ?? throw new Xunit.Sdk.XunitException($"host exited ({_process.ExitCode}) before '{prefix}'");
                }
                await Task.Delay(200);
            }
            throw new TimeoutException($"'{prefix}' not seen within {timeout}");
        }

        public async Task SendAsync(string line)
        {
            await _process.StandardInput.WriteLineAsync(line);
            await _process.StandardInput.FlushAsync();
        }

        public async Task<int> WaitForExitAsync(TimeSpan timeout)
        {
            using var cts = new CancellationTokenSource(timeout);
            await _process.WaitForExitAsync(cts.Token);
            return _process.ExitCode;
        }

        public void KillHard() => _process.Kill();

        public static IReadOnlyList<int> ParsePids(string running, string? only = null)
        {
            var keys = only is null ? new[] { "BACKEND_PIDS=", "FRONTEND_PIDS=" } : new[] { only };
            return running.Split(' ')
                .Where(token => keys.Any(key => token.StartsWith(key, StringComparison.Ordinal)))
                .SelectMany(token => token[(token.IndexOf('=') + 1)..].Split(',', StringSplitOptions.RemoveEmptyEntries))
                .Select(int.Parse)
                .ToList();
        }

        public async ValueTask DisposeAsync()
        {
            if (!_process.HasExited)
            {
                await SendAsync("tray-exit");
                try
                {
                    await WaitForExitAsync(StopTimeout);
                }
                catch (OperationCanceledException)
                {
                    _process.Kill();
                }
            }
            _process.Dispose();
        }
    }
}
