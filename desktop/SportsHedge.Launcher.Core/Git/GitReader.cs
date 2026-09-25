using System.Diagnostics;

namespace SportsHedge.Launcher.Core.Git;

public sealed record GitIdentity(string Sha, string Branch)
{
    public string Short => Sha.Length > 12 ? Sha[..12] : Sha;
}

/// <summary>Read-only Git queries. The controller never fetches, pulls, switches or resets.</summary>
public interface IGitReader
{
    GitIdentity ReadIdentity(string repoRoot);

    /// <summary>True when tracked or untracked (non-ignored) files under <paramref name="relativePath"/> differ from HEAD.</summary>
    bool IsPathDirty(string repoRoot, string relativePath);
}

public sealed class GitCli : IGitReader
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(15);
    private readonly string _gitExecutable;

    public GitCli(string gitExecutable = "git")
    {
        _gitExecutable = gitExecutable;
    }

    // Only these read-only subcommands are ever issued.
    internal static readonly IReadOnlyList<string[]> AllowedCommands = new[]
    {
        new[] { "rev-parse", "HEAD" },
        new[] { "rev-parse", "--abbrev-ref", "HEAD" },
        new[] { "status", "--porcelain", "--untracked-files=normal", "--" },
    };

    public GitIdentity ReadIdentity(string repoRoot)
    {
        var sha = Run(repoRoot, "rev-parse", "HEAD").Trim();
        var branch = Run(repoRoot, "rev-parse", "--abbrev-ref", "HEAD").Trim();
        if (sha.Length < 7)
        {
            throw new InvalidOperationException($"git rev-parse HEAD returned an unexpected value in {repoRoot}");
        }
        return new GitIdentity(sha.ToLowerInvariant(), branch);
    }

    public bool IsPathDirty(string repoRoot, string relativePath)
    {
        return Run(repoRoot, "status", "--porcelain", "--untracked-files=normal", "--", relativePath).Trim().Length > 0;
    }

    private string Run(string repoRoot, params string[] args)
    {
        var psi = new ProcessStartInfo(_gitExecutable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            // Never inherit the controller's stdin: on Windows a pending
            // synchronous read on an inherited pipe blocks git's handle probing.
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("-C");
        psi.ArgumentList.Add(repoRoot);
        foreach (var arg in args)
        {
            psi.ArgumentList.Add(arg);
        }
        using var process = Process.Start(psi) ?? throw new InvalidOperationException("git could not be started");
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit((int)Timeout.TotalMilliseconds))
        {
            try { process.Kill(); } catch (InvalidOperationException) { }
            throw new InvalidOperationException($"git {string.Join(' ', args)} timed out in {repoRoot}");
        }
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException($"git {string.Join(' ', args)} failed in {repoRoot}: {stderr.Result.Trim()}");
        }
        return stdout.Result;
    }
}
