using System.IO.Pipes;
using System.Text;
using SportsHedge.Launcher.Core.Ipc;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.State;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class ControllerIpcProtocolTests
{
    private readonly string _token = SessionSecrets.NewToken();
    private readonly List<ShutdownSource> _requests = new();
    private readonly List<string> _audit = new();
    private ShutdownRequestResult _next = ShutdownRequestResult.Accepted;

    private string Send(object payload) => Handle(System.Text.Json.JsonSerializer.Serialize(payload));

    private string Handle(string line) => ControllerIpcProtocol.Handle(
        line,
        _token,
        source =>
        {
            _requests.Add(source);
            var result = _next;
            _next = ShutdownRequestResult.AlreadyStopping;
            return result;
        },
        () => ControllerState.Running,
        _audit.Add);

    [Fact]
    public void Missing_token_is_rejected()
    {
        var reply = Send(new { type = "shutdown", source = "ui" });
        Assert.Contains("\"ok\":false", reply);
        Assert.Contains("unauthorized", reply);
        Assert.Empty(_requests);
        Assert.Contains(_audit, a => a.Contains("missing_token"));
    }

    [Fact]
    public void Incorrect_token_is_rejected_without_echoing_either_token()
    {
        var wrong = SessionSecrets.NewToken();
        var reply = Send(new { type = "shutdown", token = wrong, source = "ui" });
        Assert.Contains("unauthorized", reply);
        Assert.Empty(_requests);
        Assert.DoesNotContain(wrong, reply + string.Join("", _audit));
        Assert.DoesNotContain(_token, reply + string.Join("", _audit));
    }

    [Fact]
    public void Valid_token_requests_ui_shutdown_and_duplicates_are_idempotent()
    {
        var first = Send(new { type = "shutdown", token = _token, source = "ui" });
        var second = Send(new { type = "shutdown", token = _token, source = "ui" });
        Assert.Contains("\"result\":\"accepted\"", first);
        Assert.Contains("\"result\":\"already_stopping\"", second);
        Assert.Equal(new[] { ShutdownSource.Ui, ShutdownSource.Ui }, _requests);
        Assert.DoesNotContain(_token, first);
    }

    [Fact]
    public void Status_requires_token_and_never_shuts_down()
    {
        Assert.Contains("unauthorized", Send(new { type = "status" }));
        var reply = Send(new { type = "status", token = _token });
        Assert.Contains("\"state\":\"Running\"", reply);
        Assert.Empty(_requests);
    }

    [Fact]
    public void Malformed_oversized_and_unknown_requests_are_rejected()
    {
        Assert.Contains("bad_request", Handle("not json"));
        Assert.Contains("bad_request", Handle("{}"));
        Assert.Contains("bad_request", Handle(new string('a', ControllerIpcProtocol.MaxRequestBytes + 1)));
        Assert.Contains("unknown_command", Send(new { type = "kill", token = _token }));
        Assert.Empty(_requests);
    }

    [Fact]
    public void Token_comparison_is_exact()
    {
        Assert.True(SessionSecrets.TokenEquals(_token, _token));
        Assert.False(SessionSecrets.TokenEquals(_token + "x", _token));
        Assert.False(SessionSecrets.TokenEquals("", _token));
        Assert.False(SessionSecrets.TokenEquals(null, _token));
        Assert.False(SessionSecrets.TokenEquals(_token.ToUpperInvariant() == _token ? _token.ToLowerInvariant() : _token.ToUpperInvariant(), _token));
    }
}

public sealed class ControllerPipeServerTests
{
    [Fact]
    public async Task Pipe_round_trips_one_request_per_connection()
    {
        var secrets = SessionSecrets.Create();
        var log = new RecordingLog();
        var received = new List<string>();
        var server = new ControllerPipeServer(secrets.PipeName, line =>
        {
            lock (received) received.Add(line);
            return "{\"ok\":true,\"result\":\"accepted\",\"state\":\"Stopping\"}";
        }, log);
        server.Start();
        try
        {
            for (var i = 0; i < 2; i++)
            {
                using var client = new NamedPipeClientStream(".", secrets.PipeName, PipeDirection.InOut, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await client.ConnectAsync(5000);
                var request = Encoding.UTF8.GetBytes("{\"type\":\"shutdown\",\"token\":\"t\"}\n");
                await client.WriteAsync(request);
                await client.FlushAsync();
                using var reader = new StreamReader(client, Encoding.UTF8, leaveOpen: true);
                var reply = await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(5));
                Assert.Equal("{\"ok\":true,\"result\":\"accepted\",\"state\":\"Stopping\"}", reply);
            }
        }
        finally
        {
            await server.DisposeAsync();
        }
        Assert.Equal(2, received.Count);
        Assert.All(received, line => Assert.StartsWith("{\"type\":\"shutdown\"", line, StringComparison.Ordinal));
    }

    [Fact]
    public async Task Oversized_or_unterminated_requests_are_dropped_without_invoking_handler()
    {
        var secrets = SessionSecrets.Create();
        var log = new RecordingLog();
        var invoked = false;
        var server = new ControllerPipeServer(secrets.PipeName, _ => { invoked = true; return "{}"; }, log);
        server.Start();
        try
        {
            using var client = new NamedPipeClientStream(".", secrets.PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
            await client.ConnectAsync(5000);
            await client.WriteAsync(new byte[ControllerIpcProtocol.MaxRequestBytes + 10]);
            await client.FlushAsync();
            var buffer = new byte[16];
            try
            {
                var read = await client.ReadAsync(buffer).AsTask().WaitAsync(TimeSpan.FromSeconds(8));
                Assert.Equal(0, read);
            }
            catch (IOException)
            {
                // Server dropped the connection (reset on Unix sockets).
            }
        }
        finally
        {
            await server.DisposeAsync();
        }
        Assert.False(invoked);
        Assert.True(log.Contains("ipc_rejected"));
    }

    [WindowsFact]
    public void Windows_pipe_acl_is_current_user_only_and_denies_network_logons()
    {
        if (OperatingSystem.IsWindows())
        {
            AssertWindowsAcl();
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    private static void AssertWindowsAcl()
    {
        var secrets = SessionSecrets.Create();
        using var server = ControllerPipeServer.CreateServerStream(secrets.PipeName);
        var rules = server.GetAccessControl()
            .GetAccessRules(true, false, typeof(System.Security.Principal.SecurityIdentifier))
            .Cast<System.IO.Pipes.PipeAccessRule>()
            .ToList();
        var user = System.Security.Principal.WindowsIdentity.GetCurrent().User!;
        var network = new System.Security.Principal.SecurityIdentifier(System.Security.Principal.WellKnownSidType.NetworkSid, null);

        Assert.Contains(rules, r => r.IdentityReference.Equals(network) && r.AccessControlType == System.Security.AccessControl.AccessControlType.Deny);
        var allows = rules.Where(r => r.AccessControlType == System.Security.AccessControl.AccessControlType.Allow).ToList();
        Assert.NotEmpty(allows);
        Assert.All(allows, r => Assert.Equal(user, r.IdentityReference));
    }
}
