using System.IO.Pipes;
using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using SportsHedge.Launcher.Core.Logging;

namespace SportsHedge.Launcher.Core.Ipc;

/// <summary>
/// Private named pipe that the Next.js server process uses to ask this
/// controller to exit. Windows ACL: current user only, network logons denied.
/// Requests are also token-authenticated by <see cref="ControllerIpcProtocol"/>.
/// </summary>
public sealed class ControllerPipeServer : IAsyncDisposable
{
    private static readonly TimeSpan ConnectionTimeout = TimeSpan.FromSeconds(5);
    private readonly string _pipeName;
    private readonly Func<string, string> _handler;
    private readonly ILog _log;
    private readonly CancellationTokenSource _stop = new();
    private Task? _loop;

    public ControllerPipeServer(string pipeName, Func<string, string> handler, ILog log)
    {
        _pipeName = pipeName;
        _handler = handler;
        _log = log;
    }

    public void Start()
    {
        // Create the first instance synchronously so a squatter or failure is
        // detected before the interface starts.
        var first = CreateServerStream(_pipeName);
        _loop = Task.Run(() => AcceptLoopAsync(first, _stop.Token));
    }

    internal static NamedPipeServerStream CreateServerStream(string pipeName)
    {
        if (OperatingSystem.IsWindows())
        {
            return CreateWindowsServerStream(pipeName);
        }
        return new NamedPipeServerStream(
            pipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
    }

    [SupportedOSPlatform("windows")]
    private static NamedPipeServerStream CreateWindowsServerStream(string pipeName)
    {
        var user = WindowsIdentity.GetCurrent().User
            ?? throw new InvalidOperationException("Could not resolve the current Windows user SID");
        var security = new PipeSecurity();
        security.SetOwner(user);
        security.AddAccessRule(new PipeAccessRule(user, PipeAccessRights.FullControl, AccessControlType.Allow));
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.NetworkSid, null), PipeAccessRights.FullControl, AccessControlType.Deny));
        return NamedPipeServerStreamAcl.Create(
            pipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous,
            ControllerIpcProtocol.MaxRequestBytes, ControllerIpcProtocol.MaxRequestBytes, security);
    }

    private async Task AcceptLoopAsync(NamedPipeServerStream first, CancellationToken cancellationToken)
    {
        var next = first;
        while (!cancellationToken.IsCancellationRequested)
        {
            var server = next;
            try
            {
                await server.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);
                await HandleConnectionAsync(server, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                await server.DisposeAsync().ConfigureAwait(false);
                return;
            }
            catch (IOException ex)
            {
                _log.Warn($"ipc_connection_error type={ex.GetType().Name}");
            }
            await server.DisposeAsync().ConfigureAwait(false);
            try
            {
                next = CreateServerStream(_pipeName);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                _log.Error($"ipc_pipe_recreate_failed type={ex.GetType().Name}; UI exit unavailable, tray Exit still works");
                return;
            }
        }
        await next.DisposeAsync().ConfigureAwait(false);
    }

    private async Task HandleConnectionAsync(NamedPipeServerStream server, CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(ConnectionTimeout);
        var buffer = new byte[ControllerIpcProtocol.MaxRequestBytes + 1];
        var total = 0;
        var newline = -1;
        while (total < buffer.Length)
        {
            int read;
            try
            {
                read = await server.ReadAsync(buffer.AsMemory(total), timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                _log.Warn("ipc_rejected reason=read_timeout");
                return;
            }
            if (read == 0)
            {
                break;
            }
            newline = Array.IndexOf(buffer, (byte)'\n', total, read);
            total += read;
            if (newline >= 0)
            {
                break;
            }
        }
        if (newline < 0)
        {
            _log.Warn("ipc_rejected reason=unterminated_or_oversized_request");
            return;
        }
        var reply = _handler(Encoding.UTF8.GetString(buffer, 0, newline).TrimEnd('\r'));
        var bytes = Encoding.UTF8.GetBytes(reply + "\n");
        await server.WriteAsync(bytes, timeout.Token).ConfigureAwait(false);
        await server.FlushAsync(timeout.Token).ConfigureAwait(false);
        if (OperatingSystem.IsWindows())
        {
            server.WaitForPipeDrain();
        }
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        if (_loop is not null)
        {
            try
            {
                await _loop.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
        }
        _stop.Dispose();
    }
}
