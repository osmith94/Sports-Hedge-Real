using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.ProcessEnvironment;
using SportsHedge.Launcher.Core.Security;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class ChildEnvironmentTests
{
    private static readonly GitIdentity Git = new("abc1230000000000000000000000000000000000", "main");
    private readonly SessionSecrets _secrets = SessionSecrets.Create();
    private readonly RuntimeLayout _layout = TestLayouts.Create();

    private static readonly Dictionary<string, string> HostileParent = new()
    {
        ["PATH"] = "/usr/bin",
        ["NODE_ENV"] = "production",
        ["npm_config_omit"] = "dev",
        ["SPORTS_HEDGE_MODE"] = "live",
        ["SPORTS_HEDGE_EXECUTION_ENABLED"] = "true",
        ["PAPER_AUTOFILL_ENABLED"] = "false",
        ["SPORTS_HEDGE_CONTROLLER_TOKEN"] = "stale-token-from-shell",
        ["SPORTS_HEDGE_DESKTOP_SHUTDOWN_TOKEN"] = "stale-backend-token",
        ["NEXT_PUBLIC_SPORTS_HEDGE_CONTROLLER_TOKEN"] = "public-leak",
    };

    private Dictionary<string, string> Build(ChildRole role) =>
        ChildEnvironment.Build(role, HostileParent, _secrets, Git, _layout);

    [Theory]
    [InlineData(ChildRole.Backend)]
    [InlineData(ChildRole.Frontend)]
    [InlineData(ChildRole.FrontendBuild)]
    [InlineData(ChildRole.FrontendInstall)]
    public void Paper_only_safety_is_forced_over_parent_environment(ChildRole role)
    {
        var env = Build(role);
        Assert.Equal("paper", env["SPORTS_HEDGE_MODE"]);
        Assert.Equal("false", env["SPORTS_HEDGE_EXECUTION_ENABLED"]);
        Assert.Equal("true", env["PAPER_AUTOFILL_ENABLED"]);
        Assert.Equal("true", env["PAPER_AUTO_UNWIND_ENABLED"]);
        Assert.Equal("true", env["PAPER_LIVE_REFRESH_ENABLED"]);
        Assert.Equal("true", env["ACCOUNTING_SCHEDULE_ENABLED"]);
        Assert.Equal(Git.Sha, env["SPORTS_HEDGE_GIT_SHA"]);
        Assert.Equal("main", env["SPORTS_HEDGE_GIT_BRANCH"]);
        Assert.Equal("http://127.0.0.1:8000", env["NEXT_PUBLIC_SPORTS_HEDGE_API_URL"]);
        Assert.Equal("/usr/bin", env["PATH"]);
        Assert.DoesNotContain(env.Values, v => v is "stale-token-from-shell" or "stale-backend-token" or "public-leak");
    }

    [Fact]
    public void Backend_receives_only_its_shutdown_secret()
    {
        var env = Build(ChildRole.Backend);
        Assert.Equal("1", env[ChildEnvironment.DesktopModeEnv]);
        Assert.Equal(_secrets.BackendShutdownToken, env[ChildEnvironment.DesktopShutdownTokenEnv]);
        Assert.Equal(_secrets.SessionId, env[ChildEnvironment.DesktopSessionIdEnv]);
        Assert.DoesNotContain(_secrets.ControllerToken, env.Values);
        Assert.False(env.ContainsKey(ChildEnvironment.ControllerTokenEnv));
    }

    [Fact]
    public void Frontend_server_receives_only_the_controller_secret_and_never_as_next_public()
    {
        var env = Build(ChildRole.Frontend);
        Assert.Equal(_secrets.ControllerToken, env[ChildEnvironment.ControllerTokenEnv]);
        Assert.Equal(_secrets.PipePath, env[ChildEnvironment.ControllerPipeEnv]);
        Assert.DoesNotContain(_secrets.BackendShutdownToken, env.Values);
        foreach (var (key, value) in env.Where(pair => pair.Key.StartsWith("NEXT_PUBLIC_", StringComparison.OrdinalIgnoreCase)))
        {
            Assert.NotEqual(_secrets.ControllerToken, value);
            Assert.NotEqual(_secrets.BackendShutdownToken, value);
            Assert.DoesNotContain(_secrets.PipeName, value);
            Assert.DoesNotContain("CONTROLLER", key, StringComparison.OrdinalIgnoreCase);
        }
    }

    [Fact]
    public void Npm_ci_environment_never_omits_dev_dependencies()
    {
        var env = Build(ChildRole.FrontendInstall);
        Assert.DoesNotContain(env.Keys, k => k.Equals("NODE_ENV", StringComparison.OrdinalIgnoreCase));
        Assert.DoesNotContain(env.Keys, k => k.Equals("npm_config_omit", StringComparison.OrdinalIgnoreCase));
        Assert.DoesNotContain(_secrets.ControllerToken, env.Values);
        Assert.DoesNotContain(_secrets.BackendShutdownToken, env.Values);
    }

    [Fact]
    public void Frontend_build_environment_contains_no_secret_or_desktop_identity()
    {
        // `next build` inlines env into browser bundles; nothing private may be present.
        var env = Build(ChildRole.FrontendBuild);
        Assert.DoesNotContain(_secrets.ControllerToken, env.Values);
        Assert.DoesNotContain(_secrets.BackendShutdownToken, env.Values);
        Assert.DoesNotContain(env.Keys, k => k.StartsWith("SPORTS_HEDGE_CONTROLLER_", StringComparison.Ordinal));
        Assert.DoesNotContain(env.Keys, k => k.StartsWith("SPORTS_HEDGE_DESKTOP_", StringComparison.Ordinal));
    }
}

public sealed class SecretsAndLoggingTests
{
    [Fact]
    public void Per_launch_secrets_are_random_url_safe_and_distinct()
    {
        var a = SessionSecrets.Create();
        var b = SessionSecrets.Create();
        Assert.NotEqual(a.ControllerToken, b.ControllerToken);
        Assert.NotEqual(a.ControllerToken, a.BackendShutdownToken);
        Assert.NotEqual(a.PipeName, b.PipeName);
        Assert.Equal(43, a.ControllerToken.Length);
        Assert.Matches("^[A-Za-z0-9_-]+$", a.ControllerToken);
        Assert.StartsWith("sports-hedge-controller-", a.PipeName, StringComparison.Ordinal);
        Assert.DoesNotContain(a.SessionId, a.PipeName);
        Assert.DoesNotContain(a.ControllerToken, a.ToString());
        if (OperatingSystem.IsWindows())
        {
            Assert.StartsWith(@"\\.\pipe\sports-hedge-controller-", a.PipePath, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void Controller_log_never_writes_registered_secrets()
    {
        var layout = TestLayouts.Create();
        var secrets = SessionSecrets.Create();
        var redactor = new SecretRedactor();
        redactor.Register(secrets.ControllerToken);
        redactor.Register(secrets.BackendShutdownToken);
        var mirrored = new List<string>();
        var log = new FileControllerLog(layout.ControllerLog, redactor, mirrored.Add);

        log.Info($"accidental {secrets.ControllerToken} and {secrets.BackendShutdownToken}");

        var text = File.ReadAllText(layout.ControllerLog);
        Assert.DoesNotContain(secrets.ControllerToken, text);
        Assert.DoesNotContain(secrets.BackendShutdownToken, text);
        Assert.Contains(SecretRedactor.Marker, text);
        Assert.DoesNotContain(secrets.ControllerToken, string.Join("", mirrored));
    }

    [Fact]
    public async Task Session_logs_never_contain_secrets_across_a_full_lifecycle()
    {
        var h = new Harness();
        Assert.True((await h.Controller.StartAsync()).Success);
        h.IpcRequest("shutdown", h.Secrets.ControllerToken);
        await h.Controller.Completion.WaitAsync(TimeSpan.FromSeconds(10));
        var all = string.Join("\n", h.Log.Lines);
        Assert.DoesNotContain(h.Secrets.ControllerToken, all);
        Assert.DoesNotContain(h.Secrets.BackendShutdownToken, all);
    }
}
