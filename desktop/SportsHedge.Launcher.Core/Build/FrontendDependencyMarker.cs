using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace SportsHedge.Launcher.Core.Build;

/// <summary>
/// frontend\node_modules\.sports-hedge-deps.json. Records the SHA-256 of the
/// package-lock.json that the current node_modules was installed from. Deleted
/// before every `npm ci`, written only after a successful one. The same schema
/// is written by scripts\windows\Build-SportsHedge-App.ps1.
/// </summary>
public sealed record FrontendDependencyMarker
{
    public const string CurrentSchema = "sports-hedge-frontend-deps/v1";

    [JsonPropertyName("schema")] public string Schema { get; init; } = CurrentSchema;
    [JsonPropertyName("package_lock_sha256")] public string PackageLockSha256 { get; init; } = "";
    [JsonPropertyName("installed_at")] public string InstalledAt { get; init; } = "";
    [JsonPropertyName("installer")] public string Installer { get; init; } = "";
}

public static class FrontendDependencyMarkerStore
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };

    public static FrontendDependencyMarker? Read(string path)
    {
        try
        {
            return File.Exists(path) ? JsonSerializer.Deserialize<FrontendDependencyMarker>(File.ReadAllText(path)) : null;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public static void Write(string path, FrontendDependencyMarker marker)
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

    /// <summary>Lowercase hex SHA-256 of the lockfile bytes; null when the lockfile is missing.</summary>
    public static string? Fingerprint(string packageLockPath)
    {
        if (!File.Exists(packageLockPath))
        {
            return null;
        }
        using var stream = File.OpenRead(packageLockPath);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }
}

public enum DependencyDecisionReason
{
    Match,
    MissingNodeModules,
    MissingMarker,
    InvalidMarker,
    LockfileChanged,
}

public sealed record DependencyDecision(bool InstallRequired, DependencyDecisionReason Reason, string CurrentFingerprint, string? RecordedFingerprint)
{
    public string Describe() =>
        $"package-lock.json sha256: {CurrentFingerprint}; node_modules installed from: {RecordedFingerprint ?? "(unknown)"}; " +
        $"install={InstallRequired} reason={Reason}";
}

public static class FrontendDependencyDecision
{
    /// <summary>
    /// `npm ci` is required unless node_modules exists and was recorded as
    /// installed from exactly the current package-lock.json.
    /// </summary>
    public static DependencyDecision Evaluate(bool nodeModulesExists, FrontendDependencyMarker? marker, string currentFingerprint)
    {
        if (!nodeModulesExists)
        {
            return new DependencyDecision(true, DependencyDecisionReason.MissingNodeModules, currentFingerprint, null);
        }
        if (marker is null)
        {
            return new DependencyDecision(true, DependencyDecisionReason.MissingMarker, currentFingerprint, null);
        }
        if (marker.Schema != FrontendDependencyMarker.CurrentSchema || string.IsNullOrWhiteSpace(marker.PackageLockSha256))
        {
            return new DependencyDecision(true, DependencyDecisionReason.InvalidMarker, currentFingerprint, null);
        }
        if (!string.Equals(marker.PackageLockSha256.Trim(), currentFingerprint, StringComparison.OrdinalIgnoreCase))
        {
            return new DependencyDecision(true, DependencyDecisionReason.LockfileChanged, currentFingerprint, marker.PackageLockSha256);
        }
        return new DependencyDecision(false, DependencyDecisionReason.Match, currentFingerprint, marker.PackageLockSha256);
    }
}
