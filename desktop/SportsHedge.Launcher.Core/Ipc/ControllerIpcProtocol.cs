using System.Text.Json;
using System.Text.Json.Serialization;
using SportsHedge.Launcher.Core.Security;
using SportsHedge.Launcher.Core.State;

namespace SportsHedge.Launcher.Core.Ipc;

/// <summary>
/// One newline-terminated JSON request per connection:
/// <c>{"type":"shutdown"|"status","token":"...","source":"ui"}</c>.
/// The reply never contains the token.
/// </summary>
public static class ControllerIpcProtocol
{
    public const int MaxRequestBytes = 4096;

    public sealed record Reply(
        [property: JsonPropertyName("ok")] bool Ok,
        [property: JsonPropertyName("result")] string Result,
        [property: JsonPropertyName("state")] string State);

    private sealed record Request(
        [property: JsonPropertyName("type")] string? Type,
        [property: JsonPropertyName("token")] string? Token,
        [property: JsonPropertyName("source")] string? Source);

    public static string Handle(
        string requestLine,
        string expectedToken,
        Func<ShutdownSource, ShutdownRequestResult> requestShutdown,
        Func<ControllerState> currentState,
        Action<string>? audit = null)
    {
        Request? request;
        try
        {
            request = requestLine.Length > MaxRequestBytes ? null : JsonSerializer.Deserialize<Request>(requestLine);
        }
        catch (JsonException)
        {
            request = null;
        }

        if (request is null || string.IsNullOrWhiteSpace(request.Type))
        {
            audit?.Invoke("ipc_rejected reason=malformed_request");
            return Serialize(new Reply(false, "bad_request", currentState().ToString()));
        }
        if (string.IsNullOrEmpty(request.Token))
        {
            audit?.Invoke($"ipc_rejected reason=missing_token type={Sanitize(request.Type)}");
            return Serialize(new Reply(false, "unauthorized", currentState().ToString()));
        }
        if (!SessionSecrets.TokenEquals(request.Token, expectedToken))
        {
            audit?.Invoke($"ipc_rejected reason=invalid_token type={Sanitize(request.Type)}");
            return Serialize(new Reply(false, "unauthorized", currentState().ToString()));
        }

        switch (request.Type)
        {
            case "status":
                return Serialize(new Reply(true, "status", currentState().ToString()));
            case "shutdown":
                var result = requestShutdown(ShutdownSource.Ui);
                audit?.Invoke($"ipc_shutdown source=Ui result={result}");
                return Serialize(new Reply(true, result switch
                {
                    ShutdownRequestResult.Accepted => "accepted",
                    ShutdownRequestResult.AlreadyStopping => "already_stopping",
                    _ => "already_stopped",
                }, currentState().ToString()));
            default:
                audit?.Invoke($"ipc_rejected reason=unknown_type type={Sanitize(request.Type)}");
                return Serialize(new Reply(false, "unknown_command", currentState().ToString()));
        }
    }

    private static string Serialize(Reply reply) => JsonSerializer.Serialize(reply);

    private static string Sanitize(string value) =>
        new(value.Take(32).Select(c => char.IsLetterOrDigit(c) || c is '_' or '-' ? c : '?').ToArray());
}
