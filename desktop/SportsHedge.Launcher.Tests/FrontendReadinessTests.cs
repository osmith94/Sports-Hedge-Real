using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.State;
using Xunit;

namespace SportsHedge.Launcher.Tests;

/// <summary>
/// Minimal loopback HTTP/1.1 server standing in for both the backend and the
/// Next.js interface. The homepage "/" never answers, modelling an operator
/// console that takes longer to render than any startup timeout.
/// </summary>
public sealed class StubSportsHedgeServer : IDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _accept;

    public StubSportsHedgeServer()
    {
        _listener.Start();
        BaseUrl = $"http://127.0.0.1:{((IPEndPoint)_listener.LocalEndpoint).Port}";
        _accept = Task.Run(AcceptLoopAsync);
    }

    public string BaseUrl { get; }
    public ConcurrentQueue<string> Requests { get; } = new();
    public Func<(int Status, string Body)> BackendHealth { get; set; } = () => (503, "{}");
    public Func<(int Status, string Body)> BackendSession { get; set; } = () => (503, "{}");
    public Func<(int Status, string Body)> FrontendStatus { get; set; } = () => (503, "{}");

    public bool HomepageRequested => Requests.Contains("/");

    public static string BackendHealthJson(string sha) =>
        $"{{\"status\":\"ok\",\"mode\":\"paper\",\"execution_enabled\":false,\"build\":{{\"git_sha\":\"{sha}\"}}}}";

    public static string BackendSessionJson(string sessionId) =>
        $"{{\"desktop_mode\":true,\"session_id\":\"{sessionId}\"}}";

    public static string FrontendStatusJson(bool desktopController, string? sessionId, string? sha) =>
        $"{{\"desktop_controller\":{(desktopController ? "true" : "false")},\"session_id\":{Json(sessionId)}," +
        $"\"git_sha\":{Json(sha)},\"mode\":\"paper\",\"execution_enabled\":false}}";

    private static string Json(string? value) => value is null ? "null" : $"\"{value}\"";

    private async Task AcceptLoopAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(_cts.Token).ConfigureAwait(false);
            }
            catch (Exception) when (_cts.IsCancellationRequested)
            {
                return;
            }
            _ = Task.Run(() => ServeAsync(client));
        }
    }

    private async Task ServeAsync(TcpClient client)
    {
        using (client)
        {
            try
            {
                var stream = client.GetStream();
                var path = await ReadRequestPathAsync(stream).ConfigureAwait(false);
                Requests.Enqueue(path);
                (int Status, string Body)? response = path switch
                {
                    "/health" => BackendHealth(),
                    "/desktop/session" => BackendSession(),
                    HttpHealthProbe.FrontendStatusPath => FrontendStatus(),
                    "/" => null,
                    _ => (404, "{}"),
                };
                if (response is null)
                {
                    await Task.Delay(Timeout.Infinite, _cts.Token).ConfigureAwait(false);
                    return;
                }
                var body = Encoding.UTF8.GetBytes(response.Value.Body);
                var head = Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 {response.Value.Status} X\r\nContent-Type: application/json\r\n" +
                    $"Content-Length: {body.Length}\r\nConnection: close\r\n\r\n");
                await stream.WriteAsync(head, _cts.Token).ConfigureAwait(false);
                await stream.WriteAsync(body, _cts.Token).ConfigureAwait(false);
            }
            catch (Exception) when (_cts.IsCancellationRequested)
            {
            }
            catch (IOException)
            {
            }
        }
    }

    private async Task<string> ReadRequestPathAsync(NetworkStream stream)
    {
        var buffer = new byte[4096];
        var received = new StringBuilder();
        while (!received.ToString().Contains("\r\n\r\n", StringComparison.Ordinal))
        {
            var read = await stream.ReadAsync(buffer, _cts.Token).ConfigureAwait(false);
            if (read == 0)
            {
                break;
            }
            received.Append(Encoding.ASCII.GetString(buffer, 0, read));
        }
        var requestLine = received.ToString().Split("\r\n", 2)[0].Split(' ');
        return requestLine.Length > 1 ? requestLine[1] : "";
    }

    public void Dispose()
    {
        _cts.Cancel();
        _listener.Stop();
        try
        {
            _accept.Wait(TimeSpan.FromSeconds(2));
        }
        catch (AggregateException)
        {
        }
        _cts.Dispose();
    }
}

/// <summary>
/// Startup readiness of the interface is the lightweight /api/desktop/status
/// identity check; a slow operator homepage must not fail or gate startup.
/// </summary>
public sealed class FrontendReadinessTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static (Harness Harness, StubSportsHedgeServer Server, HttpHealthProbe Probe) Create()
    {
        var server = new StubSportsHedgeServer();
        var probe = new HttpHealthProbe(server.BaseUrl, server.BaseUrl, TimeSpan.FromSeconds(2));
        var h = new Harness(health: probe);
        server.BackendHealth = () => (200, StubSportsHedgeServer.BackendHealthJson(h.Git.Identity.Sha));
        server.BackendSession = () => (200, StubSportsHedgeServer.BackendSessionJson(h.Secrets.SessionId));
        return (h, server, probe);
    }

    [Fact]
    public async Task Probe_accepts_valid_desktop_status_without_requesting_the_homepage()
    {
        using var server = new StubSportsHedgeServer();
        using var probe = new HttpHealthProbe(server.BaseUrl, server.BaseUrl, TimeSpan.FromSeconds(2));
        server.FrontendStatus = () => (200, StubSportsHedgeServer.FrontendStatusJson(true, "s1", "abc123"));

        var result = await probe.CheckFrontendAsync("abc123", "s1", CancellationToken.None);

        Assert.True(result.Healthy, result.Detail);
        Assert.Equal(new[] { HttpHealthProbe.FrontendStatusPath }, server.Requests.ToArray());
        Assert.False(server.HomepageRequested);
    }

    [Fact]
    public async Task Startup_reaches_running_when_status_is_valid_even_though_homepage_never_answers()
    {
        var (h, server, probe) = Create();
        using var _ = server;
        using var __ = probe;
        server.FrontendStatus = () => (200, StubSportsHedgeServer.FrontendStatusJson(true, h.Secrets.SessionId, h.Git.Identity.Sha));

        var result = await h.Controller.StartAsync();

        Assert.True(result.Success, result.Reason);
        Assert.Equal(ControllerState.Running, h.Controller.State.State);
        Assert.Contains(HttpHealthProbe.FrontendStatusPath, server.Requests);
        Assert.False(server.HomepageRequested);
        Assert.True(h.Log.Contains("frontend_health healthy=True"));
        Assert.True(h.Log.Contains($"running url={LauncherConstants.AppUrl}"));

        h.Controller.RequestShutdown(ShutdownSource.Tray);
        var report = await h.Controller.Completion.WaitAsync(Wait);
        Assert.False(report.Failed);
        Assert.False(server.HomepageRequested);
    }

    [Fact]
    public async Task Startup_fails_when_interface_reports_another_session()
    {
        var (h, server, probe) = Create();
        using var _ = server;
        using var __ = probe;
        server.FrontendStatus = () => (200, StubSportsHedgeServer.FrontendStatusJson(true, "someone-elses-session", h.Git.Identity.Sha));

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Contains("session mismatch", result.Reason);
        Assert.Equal(ControllerState.Failed, h.Controller.State.State);
        Assert.Contains("frontend:terminate", h.Journal.Events);
    }

    [Fact]
    public async Task Startup_fails_when_interface_is_not_in_desktop_controller_mode()
    {
        var (h, server, probe) = Create();
        using var _ = server;
        using var __ = probe;
        server.FrontendStatus = () => (200, StubSportsHedgeServer.FrontendStatusJson(false, null, null));

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Contains("desktop-controller mode", result.Reason);
    }

    [Fact]
    public async Task Startup_fails_when_interface_serves_a_different_git_sha()
    {
        var (h, server, probe) = Create();
        using var _ = server;
        using var __ = probe;
        server.FrontendStatus = () => (200, StubSportsHedgeServer.FrontendStatusJson(true, h.Secrets.SessionId, "0ld5ha"));

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Contains("Git SHA 0ld5ha", result.Reason);
    }

    [Fact]
    public async Task Startup_fails_when_status_endpoint_keeps_failing()
    {
        var (h, server, probe) = Create();
        using var _ = server;
        using var __ = probe;
        server.FrontendStatus = () => (500, "{}");

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Contains("did not become healthy", result.Reason);
        Assert.Contains("HTTP 500", result.Reason);
        Assert.False(server.HomepageRequested);
    }

    [Fact]
    public async Task Startup_fails_when_interface_process_exits_before_status_is_ready()
    {
        var h = new Harness();
        h.Health.Frontend = () =>
        {
            h.Frontend.ExitNaturally(7);
            return HealthResult.Fail("connection refused");
        };

        var result = await h.Controller.StartAsync();

        Assert.False(result.Success);
        Assert.Equal("Interface", result.FailedComponent);
        Assert.Contains("Interface process exited with code 7 before becoming healthy", result.Reason);
        Assert.Equal(ControllerState.Failed, h.Controller.State.State);
    }

    [Fact]
    public void Browser_target_remains_the_operator_homepage()
    {
        Assert.Equal("http://127.0.0.1:3000/", LauncherConstants.AppUrl);
        Assert.Equal("/api/desktop/status", HttpHealthProbe.FrontendStatusPath);
    }
}
