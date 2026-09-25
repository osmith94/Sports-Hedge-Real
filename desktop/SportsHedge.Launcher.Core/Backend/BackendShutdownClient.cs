using System.Net.Http.Headers;

namespace SportsHedge.Launcher.Core.Backend;

public sealed record BackendShutdownResult(bool Accepted, string Detail);

public interface IBackendShutdownClient
{
    Task<BackendShutdownResult> RequestShutdownAsync(string token, CancellationToken cancellationToken);
}

/// <summary>
/// Asks sports_hedge.api.desktop_host to exit gracefully (uvicorn
/// should_exit -> FastAPI lifespan cleanup). The token is sent only in the
/// Authorization header to 127.0.0.1 and is never logged.
/// </summary>
public sealed class HttpBackendShutdownClient : IBackendShutdownClient, IDisposable
{
    private readonly HttpClient _client = new(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false })
    {
        Timeout = TimeSpan.FromSeconds(5),
    };

    public async Task<BackendShutdownResult> RequestShutdownAsync(string token, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{LauncherConstants.BackendBaseUrl}/desktop/shutdown");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        try
        {
            using var response = await _client.SendAsync(request, cancellationToken).ConfigureAwait(false);
            return (int)response.StatusCode == 202
                ? new BackendShutdownResult(true, "backend accepted graceful shutdown (HTTP 202)")
                : new BackendShutdownResult(false, $"backend refused graceful shutdown (HTTP {(int)response.StatusCode})");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return new BackendShutdownResult(false, $"backend graceful shutdown request failed: {ex.GetType().Name}");
        }
    }

    public void Dispose() => _client.Dispose();
}
