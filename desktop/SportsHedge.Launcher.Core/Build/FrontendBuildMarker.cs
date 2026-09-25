using System.Text.Json;
using System.Text.Json.Serialization;

namespace SportsHedge.Launcher.Core.Build;

/// <summary>
/// frontend\.next\sports-hedge-build.json. Written only after a successful
/// production `npm run build`, deleted before every rebuild. The same schema is
/// written by scripts\windows\Build-SportsHedge-App.ps1.
/// </summary>
public sealed record FrontendBuildMarker
{
    public const string CurrentSchema = "sports-hedge-frontend-build/v1";

    [JsonPropertyName("schema")] public string Schema { get; init; } = CurrentSchema;
    [JsonPropertyName("git_sha")] public string GitSha { get; init; } = "";
    [JsonPropertyName("git_branch")] public string GitBranch { get; init; } = "";
    [JsonPropertyName("build_timestamp")] public string BuildTimestamp { get; init; } = "";
    [JsonPropertyName("next_build_id")] public string NextBuildId { get; init; } = "";
    [JsonPropertyName("frontend_dirty")] public bool FrontendDirty { get; init; }
    [JsonPropertyName("builder")] public string Builder { get; init; } = "";
}

public static class FrontendBuildMarkerStore
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };

    /// <summary>Returns null for a missing or unreadable marker (both force a rebuild).</summary>
    public static FrontendBuildMarker? Read(string path)
    {
        try
        {
            if (!File.Exists(path))
            {
                return null;
            }
            return JsonSerializer.Deserialize<FrontendBuildMarker>(File.ReadAllText(path));
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public static void Write(string path, FrontendBuildMarker marker)
    {
        var temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(marker, Options));
        File.Move(temp, path, overwrite: true);
    }

    public static void Delete(string path)
    {
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }

    public static string? ReadNextBuildId(string buildIdPath)
    {
        try
        {
            return File.Exists(buildIdPath) ? File.ReadAllText(buildIdPath).Trim() : null;
        }
        catch (IOException)
        {
            return null;
        }
    }
}

public enum BuildDecisionReason
{
    Match,
    MissingMarker,
    InvalidMarker,
    MissingNextBuild,
    NextBuildIdMismatch,
    ShaMismatch,
    DirtyAtBuild,
    DirtyNow,
}

public sealed record BuildDecision(bool RebuildRequired, BuildDecisionReason Reason, string CurrentSha, string? BuiltSha)
{
    public string Describe() =>
        $"Current checkout SHA: {CurrentSha}; Frontend built for SHA: {BuiltSha ?? "(none)"}; " +
        $"rebuild={RebuildRequired} reason={Reason}";
}

public static class FrontendBuildDecision
{
    /// <summary>
    /// SHA-based: any HEAD change rebuilds (correctness over a few seconds).
    /// Uncommitted frontend changes also rebuild, because an unchanged SHA
    /// would otherwise serve stale UI code.
    /// </summary>
    public static BuildDecision Evaluate(
        string currentSha,
        bool frontendDirtyNow,
        FrontendBuildMarker? marker,
        string? nextBuildIdOnDisk)
    {
        if (marker is null)
        {
            return new BuildDecision(true, BuildDecisionReason.MissingMarker, currentSha, null);
        }
        if (marker.Schema != FrontendBuildMarker.CurrentSchema
            || string.IsNullOrWhiteSpace(marker.GitSha)
            || string.IsNullOrWhiteSpace(marker.NextBuildId))
        {
            return new BuildDecision(true, BuildDecisionReason.InvalidMarker, currentSha, NullIfBlank(marker.GitSha));
        }
        if (string.IsNullOrWhiteSpace(nextBuildIdOnDisk))
        {
            return new BuildDecision(true, BuildDecisionReason.MissingNextBuild, currentSha, marker.GitSha);
        }
        if (!string.Equals(marker.NextBuildId.Trim(), nextBuildIdOnDisk.Trim(), StringComparison.Ordinal))
        {
            return new BuildDecision(true, BuildDecisionReason.NextBuildIdMismatch, currentSha, marker.GitSha);
        }
        if (!string.Equals(marker.GitSha.Trim(), currentSha.Trim(), StringComparison.OrdinalIgnoreCase))
        {
            return new BuildDecision(true, BuildDecisionReason.ShaMismatch, currentSha, marker.GitSha);
        }
        if (marker.FrontendDirty)
        {
            return new BuildDecision(true, BuildDecisionReason.DirtyAtBuild, currentSha, marker.GitSha);
        }
        if (frontendDirtyNow)
        {
            return new BuildDecision(true, BuildDecisionReason.DirtyNow, currentSha, marker.GitSha);
        }
        return new BuildDecision(false, BuildDecisionReason.Match, currentSha, marker.GitSha);
    }

    private static string? NullIfBlank(string value) => string.IsNullOrWhiteSpace(value) ? null : value;
}
