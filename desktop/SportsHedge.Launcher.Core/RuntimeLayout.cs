using System.Text.Json;

namespace SportsHedge.Launcher.Core;

/// <summary>Paths inside the operator's local Sports Hedge checkout.</summary>
public sealed record RuntimeLayout(string RepoRoot, bool IsWindows)
{
    public string BackendDir => Path.Combine(RepoRoot, "backend");
    public string FrontendDir => Path.Combine(RepoRoot, "frontend");
    public string LogsDir => Path.Combine(RepoRoot, "logs");
    public string NodeModulesDir => Path.Combine(FrontendDir, "node_modules");
    public string PackageJsonPath => Path.Combine(FrontendDir, "package.json");
    public string PackageLockPath => Path.Combine(FrontendDir, "package-lock.json");

    // Inside node_modules (gitignored); `npm ci` and deleting node_modules both remove it.
    public string FrontendDependencyMarkerPath => Path.Combine(NodeModulesDir, ".sports-hedge-deps.json");

    // Production Next.js output. `next dev` and the Refresh script both wipe
    // this directory, which also removes the marker below.
    public string NextDir => Path.Combine(FrontendDir, ".next");
    public string NextBuildIdPath => Path.Combine(NextDir, "BUILD_ID");
    public string FrontendBuildMarkerPath => Path.Combine(NextDir, "sports-hedge-build.json");

    public string CanonicalDotEnv => Path.Combine(RepoRoot, ".env");
    public string LegacyBackendDotEnv => Path.Combine(BackendDir, ".env");

    public string PythonExecutable => IsWindows
        ? Path.Combine(BackendDir, ".venv", "Scripts", "python.exe")
        : Path.Combine(BackendDir, ".venv", "bin", "python");

    public string ControllerLog => Path.Combine(LogsDir, "desktop-controller.log");
    public string BackendOutLog => Path.Combine(LogsDir, "desktop-backend.out.log");
    public string BackendErrLog => Path.Combine(LogsDir, "desktop-backend.err.log");
    public string FrontendOutLog => Path.Combine(LogsDir, "desktop-frontend.out.log");
    public string FrontendErrLog => Path.Combine(LogsDir, "desktop-frontend.err.log");
    public string FrontendBuildOutLog => Path.Combine(LogsDir, "desktop-frontend-build.out.log");
    public string FrontendBuildErrLog => Path.Combine(LogsDir, "desktop-frontend-build.err.log");

    public static RuntimeLayout ForCurrentPlatform(string repoRoot) =>
        new(Path.GetFullPath(repoRoot), OperatingSystem.IsWindows());
}

public sealed record RepoLocation(string? RepoRoot, string Detail)
{
    public bool Found => RepoRoot is not null;
}

/// <summary>
/// Finds the checkout SportsHedge.exe controls. Order: SPORTS_HEDGE_REPO_ROOT,
/// SportsHedge.launcher.json next to the exe (written by the build script),
/// then walking up from the exe directory (dist\windows lives in the repo).
/// </summary>
public static class RepoLocator
{
    public static bool IsSportsHedgeRepo(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !Directory.Exists(path))
        {
            return false;
        }
        var gitMarker = Path.Combine(path, ".git");
        return (Directory.Exists(gitMarker) || File.Exists(gitMarker))
            && File.Exists(Path.Combine(path, "backend", "pyproject.toml"))
            && File.Exists(Path.Combine(path, "backend", "src", "sports_hedge", "api", "desktop_host.py"))
            && File.Exists(Path.Combine(path, "frontend", "package.json"));
    }

    public static RepoLocation Locate(string exeDirectory, string? envOverride)
    {
        if (!string.IsNullOrWhiteSpace(envOverride))
        {
            var full = Path.GetFullPath(envOverride);
            return IsSportsHedgeRepo(full)
                ? new RepoLocation(full, $"{LauncherConstants.RepoRootOverrideEnv}={full}")
                : new RepoLocation(null, $"{LauncherConstants.RepoRootOverrideEnv} points at {full}, which is not a Sports Hedge checkout with the desktop host.");
        }

        var configPath = Path.Combine(exeDirectory, LauncherConstants.LauncherConfigFileName);
        if (File.Exists(configPath))
        {
            var configured = ReadConfiguredRoot(configPath);
            if (configured is not null && IsSportsHedgeRepo(configured))
            {
                return new RepoLocation(Path.GetFullPath(configured), $"{configPath} repo_root");
            }
        }

        var current = new DirectoryInfo(Path.GetFullPath(exeDirectory));
        while (current is not null)
        {
            if (IsSportsHedgeRepo(current.FullName))
            {
                return new RepoLocation(current.FullName, $"parent of {exeDirectory}");
            }
            current = current.Parent;
        }

        return new RepoLocation(null,
            $"Could not find the Sports Hedge checkout from {exeDirectory}. Run SportsHedge.exe from dist\\windows inside the repository, " +
            $"keep {LauncherConstants.LauncherConfigFileName} next to it, or set {LauncherConstants.RepoRootOverrideEnv}.");
    }

    internal static string? ReadConfiguredRoot(string configPath)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(configPath));
            return doc.RootElement.TryGetProperty("repo_root", out var root) && root.ValueKind == JsonValueKind.String
                ? root.GetString()
                : null;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }
}
