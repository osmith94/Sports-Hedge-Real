using System.Net;
using System.Net.Sockets;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Health;
using SportsHedge.Launcher.Core.Ports;
using SportsHedge.Launcher.Core.State;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class PortPreflightTests
{
    [Fact]
    public void Free_ports_pass()
    {
        Assert.True(PortPreflight.Evaluate(new FakePorts()).Ok);
    }

    [Fact]
    public void Occupied_port_refuses_with_clear_message_and_no_kill()
    {
        var ports = new FakePorts();
        ports.Occupied[8000] = new PortStatus(8000, true, 123, "python");
        var result = PortPreflight.Evaluate(ports);
        Assert.False(result.Ok);
        Assert.Equal(
            "Sports Hedge cannot start because port 8000 is already in use by another process (PID 123 (python)). " +
            "No processes were terminated. If the PowerShell demo launcher is running, stop it with " +
            @"scripts\windows\Stop-SportsHedge-Demo.bat, or close the other application, then try again.",
            result.Message);
    }

    [Fact]
    public void Real_listener_is_detected_and_left_running()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;

        var result = PortPreflight.Evaluate(new SystemPortProbe(), new[] { new PortRequirement(port, "test") });

        Assert.False(result.Ok);
        Assert.Contains($"port {port}", result.Message);
        using var client = new TcpClient();
        client.Connect(IPAddress.Loopback, port);
        Assert.True(client.Connected);
        if (OperatingSystem.IsWindows())
        {
            Assert.Equal(System.Environment.ProcessId, new SystemPortProbe().Inspect(port).OwnerPid);
        }
    }

    [Fact]
    public void Released_port_is_reported_free()
    {
        int port;
        using (var listener = new TcpListener(IPAddress.Loopback, 0))
        {
            listener.Start();
            port = ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        Assert.False(new SystemPortProbe().Inspect(port).Listening);
    }
}

public sealed class HealthEvaluatorTests
{
    private const string Sha = "abc123";

    private static string Health(string mode = "paper", string exec = "false", string sha = Sha) =>
        $"{{\"status\":\"ok\",\"mode\":\"{mode}\",\"execution_enabled\":{exec},\"build\":{{\"git_sha\":\"{sha}\"}}}}";

    [Fact]
    public void Genuine_backend_is_paper_execution_disabled_and_current_sha()
    {
        Assert.True(HealthEvaluator.EvaluateBackendHealth(Health(), Sha).Healthy);
        Assert.False(HealthEvaluator.EvaluateBackendHealth(Health(mode: "live"), Sha).Healthy);
        Assert.False(HealthEvaluator.EvaluateBackendHealth(Health(exec: "true"), Sha).Healthy);
        Assert.False(HealthEvaluator.EvaluateBackendHealth(Health(sha: "old"), Sha).Healthy);
        Assert.False(HealthEvaluator.EvaluateBackendHealth("<html>", Sha).Healthy);
    }

    [Fact]
    public void Session_id_proves_the_listener_is_this_controllers_child()
    {
        Assert.True(HealthEvaluator.EvaluateBackendSession("{\"desktop_mode\":true,\"session_id\":\"s1\"}", "s1").Healthy);
        Assert.False(HealthEvaluator.EvaluateBackendSession("{\"desktop_mode\":true,\"session_id\":\"other\"}", "s1").Healthy);
        Assert.True(HealthEvaluator.EvaluateFrontendStatus("{\"desktop_controller\":true,\"session_id\":\"s1\",\"git_sha\":\"abc123\"}", Sha, "s1").Healthy);
        Assert.False(HealthEvaluator.EvaluateFrontendStatus("{\"desktop_controller\":false,\"session_id\":null}", Sha, "s1").Healthy);
        Assert.False(HealthEvaluator.EvaluateFrontendStatus("{\"desktop_controller\":true,\"session_id\":\"s1\",\"git_sha\":\"old\"}", Sha, "s1").Healthy);
    }
}

public sealed class OperatorMessageTests
{
    [Fact]
    public void Tray_text_identifies_paper_mode_while_running()
    {
        Assert.Equal("Sports Hedge — PAPER MODE — Running", OperatorMessages.TrayText(ControllerState.Running));
        Assert.Equal("Sports Hedge — Starting", OperatorMessages.TrayText(ControllerState.Starting));
        Assert.Equal("Sports Hedge — Degraded", OperatorMessages.TrayText(ControllerState.Degraded));
        Assert.Equal("Sports Hedge — Stopping", OperatorMessages.TrayText(ControllerState.Stopping));
        Assert.All(Enum.GetValues<ControllerState>(), s => Assert.True(OperatorMessages.TrayText(s).Length <= 127));
    }

    [Fact]
    public void Exit_confirmation_matches_the_web_ui_wording()
    {
        Assert.Equal("Exit Sports Hedge?", OperatorMessages.ExitConfirmTitle);
        Assert.Equal("This will stop the scanner, backend and local interface.", OperatorMessages.ExitConfirmBody);
        var ui = File.ReadAllText(Path.Combine(TestPaths.RepoRoot, "frontend", "lib", "desktop-exit-state.ts"));
        Assert.Contains(OperatorMessages.ExitConfirmTitle, ui);
        Assert.Contains(OperatorMessages.ExitConfirmBody, ui);
    }
}
