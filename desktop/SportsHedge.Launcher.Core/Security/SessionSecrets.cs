using System.Security.Cryptography;
using System.Text;

namespace SportsHedge.Launcher.Core.Security;

/// <summary>
/// Per-launch identities. Secrets live only in controller memory and in the
/// environment block of the single child that needs each one:
/// <list type="bullet">
/// <item><see cref="ControllerToken"/>: Next.js server process only (IPC auth to this controller).</item>
/// <item><see cref="BackendShutdownToken"/>: backend process only (controller -> backend shutdown).</item>
/// </list>
/// <see cref="SessionId"/> is public and lets health checks prove the listeners
/// on 8000/3000 are the children this controller launched.
/// </summary>
public sealed class SessionSecrets
{
    public const int TokenBytes = 32;

    private SessionSecrets(string controllerToken, string backendShutdownToken, string sessionId, string pipeId)
    {
        ControllerToken = controllerToken;
        BackendShutdownToken = backendShutdownToken;
        SessionId = sessionId;
        PipeName = LauncherConstants.PipeNamePrefix + pipeId;
    }

    public string ControllerToken { get; }
    public string BackendShutdownToken { get; }
    public string SessionId { get; }
    public string PipeName { get; }

    public string PipePath => OperatingSystem.IsWindows()
        ? @"\\.\pipe\" + PipeName
        // .NET maps NamedPipeServerStream names to Unix domain sockets here.
        : Path.Combine(Path.GetTempPath(), "CoreFxPipe_" + PipeName);

    public static SessionSecrets Create() => new(
        NewToken(),
        NewToken(),
        Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant(),
        Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant());

    public static string NewToken()
    {
        return Convert.ToBase64String(RandomNumberGenerator.GetBytes(TokenBytes))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }

    public static bool TokenEquals(string? presented, string expected)
    {
        if (string.IsNullOrEmpty(presented) || string.IsNullOrEmpty(expected))
        {
            return false;
        }
        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(presented), Encoding.UTF8.GetBytes(expected));
    }

    public override string ToString() => $"SessionSecrets(session={SessionId}, secrets=[REDACTED])";
}
