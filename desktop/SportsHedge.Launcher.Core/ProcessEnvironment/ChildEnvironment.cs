using System.Collections;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Security;

namespace SportsHedge.Launcher.Core.ProcessEnvironment;

public enum ChildRole
{
    Backend,
    Frontend,
    FrontendBuild,
    FrontendInstall,
}

/// <summary>
/// Builds each child's environment block. Paper-only settings are forced last
/// so neither the operator's shell nor repo-root .env can enable execution
/// (process environment overrides dotenv in sports_hedge.config).
/// </summary>
public static class ChildEnvironment
{
    public const string ControllerPipeEnv = "SPORTS_HEDGE_CONTROLLER_PIPE";
    public const string ControllerTokenEnv = "SPORTS_HEDGE_CONTROLLER_TOKEN";
    public const string DesktopModeEnv = "SPORTS_HEDGE_DESKTOP_MODE";
    public const string DesktopShutdownTokenEnv = "SPORTS_HEDGE_DESKTOP_SHUTDOWN_TOKEN";
    public const string DesktopSessionIdEnv = "SPORTS_HEDGE_DESKTOP_SESSION_ID";
    public const string DesktopOriginsEnv = "SPORTS_HEDGE_DESKTOP_ORIGINS";

    // Mirrors scripts/windows/Start-SportsHedge-Demo.ps1.
    public static readonly IReadOnlyDictionary<string, string> PaperSafety = new Dictionary<string, string>
    {
        ["SPORTS_HEDGE_MODE"] = "paper",
        ["SPORTS_HEDGE_EXECUTION_ENABLED"] = "false",
        ["PAPER_AUTOFILL_ENABLED"] = "true",
        ["PAPER_AUTO_UNWIND_ENABLED"] = "true",
        ["PAPER_LIVE_REFRESH_ENABLED"] = "true",
        ["ACCOUNTING_SCHEDULE_ENABLED"] = "true",
    };

    private static readonly string[] ReservedPrefixes =
    {
        "SPORTS_HEDGE_CONTROLLER_",
        "SPORTS_HEDGE_DESKTOP_",
        "NEXT_PUBLIC_SPORTS_HEDGE_CONTROLLER",
        "NEXT_PUBLIC_SPORTS_HEDGE_DESKTOP",
    };

    public static Dictionary<string, string> Build(
        ChildRole role,
        IReadOnlyDictionary<string, string> parent,
        SessionSecrets secrets,
        GitIdentity git,
        RuntimeLayout layout)
    {
        var comparer = layout.IsWindows ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
        var env = new Dictionary<string, string>(comparer);
        foreach (var (key, value) in parent)
        {
            if (!ReservedPrefixes.Any(prefix => key.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)))
            {
                env[key] = value;
            }
        }

        env["NEXT_PUBLIC_SPORTS_HEDGE_API_URL"] = LauncherConstants.BackendBaseUrl;
        env["SPORTS_HEDGE_GIT_SHA"] = git.Sha;
        env["SPORTS_HEDGE_GIT_BRANCH"] = git.Branch;
        env["SPORTS_HEDGE_REPO_ROOT"] = layout.RepoRoot;
        env["NEXT_TELEMETRY_DISABLED"] = "1";

        switch (role)
        {
            case ChildRole.Backend:
                env["PYTHONUNBUFFERED"] = "1";
                env[DesktopModeEnv] = "1";
                env[DesktopShutdownTokenEnv] = secrets.BackendShutdownToken;
                env[DesktopSessionIdEnv] = secrets.SessionId;
                break;
            case ChildRole.Frontend:
                env["NODE_ENV"] = "production";
                env[ControllerPipeEnv] = secrets.PipePath;
                env[ControllerTokenEnv] = secrets.ControllerToken;
                env[DesktopSessionIdEnv] = secrets.SessionId;
                env[DesktopOriginsEnv] = LauncherConstants.DesktopOrigins;
                break;
            case ChildRole.FrontendBuild:
                // `next build` inlines NEXT_PUBLIC_* only; no desktop identity
                // or secret is present while building.
                env["NODE_ENV"] = "production";
                break;
            case ChildRole.FrontendInstall:
                // NODE_ENV=production makes `npm ci` omit devDependencies
                // (typescript, @types/*); `next build` would then run
                // `npm install` itself and rewrite package.json/package-lock.json.
                foreach (var key in env.Keys.Where(k => k.Equals("NODE_ENV", StringComparison.OrdinalIgnoreCase)
                                                        || k.Equals("NPM_CONFIG_PRODUCTION", StringComparison.OrdinalIgnoreCase)
                                                        || k.Equals("NPM_CONFIG_OMIT", StringComparison.OrdinalIgnoreCase)).ToList())
                {
                    env.Remove(key);
                }
                break;
        }

        foreach (var (key, value) in PaperSafety)
        {
            env[key] = value;
        }
        return env;
    }

    public static IReadOnlyDictionary<string, string> CurrentProcess()
    {
        var snapshot = new Dictionary<string, string>(
            OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
        foreach (DictionaryEntry entry in System.Environment.GetEnvironmentVariables())
        {
            if (entry.Key is string key && entry.Value is string value)
            {
                snapshot[key] = value;
            }
        }
        return snapshot;
    }
}
