using System.Net.Http.Headers;
using System.Text.Json;

namespace SportsHedge.Launcher.Core.Health;

public sealed record HealthResult(bool Healthy, string Detail)
{
    public static HealthResult Ok(string detail) => new(true, detail);
    public static HealthResult Fail(string detail) => new(false, detail);
}

public interface IHealthProbe
{
    Task<HealthResult> CheckBackendAsync(string expectedSha, string sessionId, CancellationToken cancellationToken);
    Task<HealthResult> CheckFrontendAsync(string expectedSha, string sessionId, bool includePage, CancellationToken cancellationToken);
}

/// <summary>
/// "Genuine" health: the listener must be PAPER / execution disabled, serve
/// the current Git SHA, and echo this controller's public session id, which
/// proves it is the child this controller launched.
/// </summary>
public static class HealthEvaluator
{
    public static HealthResult EvaluateBackendHealth(string json, string expectedSha)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (Str(root, "status") != "ok")
            {
                return HealthResult.Fail("backend /health status is not ok");
            }
            if (Str(root, "mode") != "paper")
            {
                return HealthResult.Fail($"backend reports mode={Str(root, "mode") ?? "(missing)"}; PAPER mode is required");
            }
            if (!root.TryGetProperty("execution_enabled", out var exec) || exec.ValueKind != JsonValueKind.False)
            {
                return HealthResult.Fail("backend does not report execution_enabled=false");
            }
            var sha = root.TryGetProperty("build", out var build) ? Str(build, "git_sha") : null;
            if (!string.Equals(sha, expectedSha, StringComparison.OrdinalIgnoreCase))
            {
                return HealthResult.Fail($"backend serves Git SHA {sha ?? "(unknown)"}, expected {expectedSha}");
            }
            return HealthResult.Ok("backend /health ok (paper, execution disabled, current SHA)");
        }
        catch (JsonException)
        {
            return HealthResult.Fail("backend /health returned invalid JSON");
        }
    }

    public static HealthResult EvaluateBackendSession(string json, string sessionId)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            return Str(doc.RootElement, "session_id") == sessionId
                ? HealthResult.Ok("backend session matches this controller")
                : HealthResult.Fail("backend on port 8000 was not launched by this controller (session mismatch)");
        }
        catch (JsonException)
        {
            return HealthResult.Fail("backend /desktop/session returned invalid JSON");
        }
    }

    public static HealthResult EvaluateFrontendStatus(string json, string expectedSha, string sessionId)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (!root.TryGetProperty("desktop_controller", out var desktop) || desktop.ValueKind != JsonValueKind.True)
            {
                return HealthResult.Fail("interface does not report desktop-controller mode");
            }
            if (Str(root, "session_id") != sessionId)
            {
                return HealthResult.Fail("interface on port 3000 was not launched by this controller (session mismatch)");
            }
            if (!string.Equals(Str(root, "git_sha"), expectedSha, StringComparison.OrdinalIgnoreCase))
            {
                return HealthResult.Fail($"interface reports Git SHA {Str(root, "git_sha") ?? "(unknown)"}, expected {expectedSha}");
            }
            return HealthResult.Ok("interface desktop status ok");
        }
        catch (JsonException)
        {
            return HealthResult.Fail("interface /api/desktop/status returned invalid JSON");
        }
    }

    private static string? Str(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
}

public sealed class HttpHealthProbe : IHealthProbe, IDisposable
{
    private readonly HttpClient _client;

    public HttpHealthProbe()
    {
        // Loopback only; never route through a system proxy.
        _client = new HttpClient(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false })
        {
            Timeout = TimeSpan.FromSeconds(5),
        };
        _client.DefaultRequestHeaders.CacheControl = new CacheControlHeaderValue { NoStore = true };
    }

    public async Task<HealthResult> CheckBackendAsync(string expectedSha, string sessionId, CancellationToken cancellationToken)
    {
        var health = await GetAsync($"{LauncherConstants.BackendBaseUrl}/health", cancellationToken).ConfigureAwait(false);
        if (!health.Healthy)
        {
            return health;
        }
        var verdict = HealthEvaluator.EvaluateBackendHealth(health.Detail, expectedSha);
        if (!verdict.Healthy)
        {
            return verdict;
        }
        var session = await GetAsync($"{LauncherConstants.BackendBaseUrl}/desktop/session", cancellationToken).ConfigureAwait(false);
        return session.Healthy ? HealthEvaluator.EvaluateBackendSession(session.Detail, sessionId) : session;
    }

    public async Task<HealthResult> CheckFrontendAsync(string expectedSha, string sessionId, bool includePage, CancellationToken cancellationToken)
    {
        var status = await GetAsync($"{LauncherConstants.FrontendBaseUrl}/api/desktop/status", cancellationToken).ConfigureAwait(false);
        if (!status.Healthy)
        {
            return status;
        }
        var verdict = HealthEvaluator.EvaluateFrontendStatus(status.Detail, expectedSha, sessionId);
        if (!verdict.Healthy || !includePage)
        {
            return verdict;
        }
        var page = await GetAsync(LauncherConstants.AppUrl, cancellationToken).ConfigureAwait(false);
        return page.Healthy ? HealthResult.Ok("interface serves / and desktop status") : page;
    }

    /// <summary>Healthy carries the body in Detail on success.</summary>
    private async Task<HealthResult> GetAsync(string url, CancellationToken cancellationToken)
    {
        try
        {
            using var response = await _client.GetAsync(url, cancellationToken).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            return response.IsSuccessStatusCode
                ? HealthResult.Ok(body)
                : HealthResult.Fail($"GET {url} returned HTTP {(int)response.StatusCode}");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && !cancellationToken.IsCancellationRequested)
        {
            return HealthResult.Fail($"GET {url} failed: {ex.GetType().Name}");
        }
    }

    public void Dispose() => _client.Dispose();
}
