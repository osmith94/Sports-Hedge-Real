using SportsHedge.Launcher.Core.Git;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.Processes;
using SportsHedge.Launcher.Core.Runtime;

namespace SportsHedge.Launcher.Core.Build;

public sealed record FrontendBuildResult(bool Success, string Detail);

public interface IFrontendBuilder
{
    Task<FrontendBuildResult> EnsureDependenciesAsync(GitIdentity git, CancellationToken cancellationToken);
    Task<FrontendBuildResult> BuildAsync(GitIdentity git, bool frontendDirty, CancellationToken cancellationToken);
}

/// <summary>
/// Production `npm run build` inside its own kill-on-close group. The marker
/// is deleted before building and written only after success, so a failed
/// build can never leave an old .next looking current.
/// </summary>
public sealed class NpmFrontendBuilder : IFrontendBuilder
{
    private readonly RuntimeLayout _layout;
    private readonly IProcessGroupLauncher _launcher;
    private readonly IServiceSpecFactory _specs;
    private readonly ILog _log;
    private readonly TimeSpan _timeout;

    public NpmFrontendBuilder(RuntimeLayout layout, IProcessGroupLauncher launcher, IServiceSpecFactory specs, ILog log, TimeSpan? timeout = null)
    {
        _layout = layout;
        _launcher = launcher;
        _specs = specs;
        _log = log;
        _timeout = timeout ?? TimeSpan.FromMinutes(15);
    }

    public async Task<FrontendBuildResult> EnsureDependenciesAsync(GitIdentity git, CancellationToken cancellationToken)
    {
        if (Directory.Exists(_layout.NodeModulesDir))
        {
            return new FrontendBuildResult(true, "frontend node_modules present");
        }
        _log.Info("frontend_install_start command=\"npm ci\" reason=node_modules_missing");
        var result = await RunAsync(_specs.FrontendInstall(git), "npm ci", cancellationToken).ConfigureAwait(false);
        _log.Info($"frontend_install_result success={result.Success} detail={result.Detail}");
        return result;
    }

    public async Task<FrontendBuildResult> BuildAsync(GitIdentity git, bool frontendDirty, CancellationToken cancellationToken)
    {
        FrontendBuildMarkerStore.Delete(_layout.FrontendBuildMarkerPath);
        var run = await RunAsync(_specs.FrontendBuild(git), "npm run build", cancellationToken).ConfigureAwait(false);
        if (!run.Success)
        {
            return run;
        }
        var buildId = FrontendBuildMarkerStore.ReadNextBuildId(_layout.NextBuildIdPath);
        if (string.IsNullOrWhiteSpace(buildId))
        {
            return new FrontendBuildResult(false, "npm run build exited 0 but frontend\\.next\\BUILD_ID is missing.");
        }
        FrontendBuildMarkerStore.Write(_layout.FrontendBuildMarkerPath, new FrontendBuildMarker
        {
            GitSha = git.Sha,
            GitBranch = git.Branch,
            BuildTimestamp = DateTimeOffset.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture),
            NextBuildId = buildId,
            FrontendDirty = frontendDirty,
            Builder = "SportsHedge.exe",
        });
        return new FrontendBuildResult(true, $"production build {buildId} recorded for {git.Sha}");
    }

    private async Task<FrontendBuildResult> RunAsync(ProcessSpec spec, string label, CancellationToken cancellationToken)
    {
        LogFiles.RollToPrevious(spec.StdoutLog);
        LogFiles.RollToPrevious(spec.StderrLog);
        using var group = _launcher.Start(spec);
        _log.Info($"{spec.Label}_started root_pid={group.RootProcessId} command=\"{label}\"");
        try
        {
            var finished = await Task.WhenAny(group.RootExited, Task.Delay(_timeout, cancellationToken)).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (finished != group.RootExited)
            {
                group.TerminateAll();
                return new FrontendBuildResult(false, $"{label} did not finish within {_timeout.TotalMinutes:0} minutes.");
            }
            var exitCode = group.RootExitCode ?? -1;
            await group.WaitForAllExitedAsync(TimeSpan.FromSeconds(10), CancellationToken.None).ConfigureAwait(false);
            group.TerminateAll();
            if (exitCode != 0)
            {
                var tail = LogFiles.Tail(spec.StderrLog);
                if (string.IsNullOrWhiteSpace(tail))
                {
                    tail = LogFiles.Tail(spec.StdoutLog);
                }
                return new FrontendBuildResult(false, $"{label} failed with exit code {exitCode}. See {spec.StdoutLog}.\n{tail}");
            }
            return new FrontendBuildResult(true, $"{label} succeeded");
        }
        catch (OperationCanceledException)
        {
            group.TerminateAll();
            throw;
        }
    }
}
