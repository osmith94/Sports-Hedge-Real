using System.Diagnostics;

namespace SportsHedge.Launcher.Core.Processes;

/// <summary>
/// Development/CI fallback for non-Windows hosts so the controller state
/// machine can be exercised end-to-end. It has NO kill-on-close guarantee:
/// if the controller is killed, children survive. SportsHedge.exe never uses it.
/// </summary>
public sealed class PosixProcessGroupLauncher : IProcessGroupLauncher
{
    public bool ProvidesKillOnClose => false;

    public IOwnedProcessGroup Start(ProcessSpec spec)
    {
        var psi = new ProcessStartInfo(spec.FileName)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = spec.WorkingDirectory,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var arg in spec.Arguments)
        {
            psi.ArgumentList.Add(arg);
        }
        psi.Environment.Clear();
        foreach (var (key, value) in spec.Environment)
        {
            psi.Environment[key] = value;
        }
        var process = Process.Start(psi) ?? throw new InvalidOperationException($"could not start {spec.FileName}");
        process.StandardInput.Close();
        return new PosixProcessGroup(spec.Label, process, spec.StdoutLog, spec.StderrLog);
    }

    private sealed class PosixProcessGroup : IOwnedProcessGroup
    {
        private readonly Process _process;
        private readonly Task _pumps;

        public PosixProcessGroup(string label, Process process, string stdoutLog, string stderrLog)
        {
            Label = label;
            _process = process;
            RootProcessId = process.Id;
            RootExited = process.WaitForExitAsync();
            _pumps = Task.WhenAll(
                Pump(process.StandardOutput.BaseStream, stdoutLog),
                Pump(process.StandardError.BaseStream, stderrLog));
        }

        public string Label { get; }
        public int RootProcessId { get; }
        public bool RootHasExited => _process.HasExited;
        public int? RootExitCode => _process.HasExited ? _process.ExitCode : null;
        public Task RootExited { get; }

        public IReadOnlyList<int> ActiveProcessIds()
        {
            var live = new List<int>();
            if (!_process.HasExited)
            {
                live.Add(RootProcessId);
            }
            // Re-parented orphans are not tracked: never risk acting on a reused PID.
            live.AddRange(DescendantsOf(RootProcessId));
            return live;
        }

        public void TerminateAll()
        {
            var targets = ActiveProcessIds();
            try
            {
                if (!_process.HasExited)
                {
                    _process.Kill(entireProcessTree: true);
                }
            }
            catch (InvalidOperationException)
            {
            }
            foreach (var pid in targets)
            {
                try
                {
                    using var p = Process.GetProcessById(pid);
                    p.Kill(entireProcessTree: true);
                }
                catch (ArgumentException)
                {
                }
                catch (InvalidOperationException)
                {
                }
            }
        }

        public async Task<bool> WaitForAllExitedAsync(TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            var deadline = DateTime.UtcNow + timeout;
            while (ActiveProcessIds().Count > 0)
            {
                if (DateTime.UtcNow >= deadline)
                {
                    return false;
                }
                await Task.Delay(150, cancellationToken).ConfigureAwait(false);
            }
            return true;
        }

        public void Dispose()
        {
            try
            {
                _pumps.Wait(TimeSpan.FromSeconds(2));
            }
            catch (AggregateException)
            {
            }
            _process.Dispose();
        }

        private static async Task Pump(Stream source, string path)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            await using var target = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
            var buffer = new byte[8192];
            int read;
            while ((read = await source.ReadAsync(buffer).ConfigureAwait(false)) > 0)
            {
                await target.WriteAsync(buffer.AsMemory(0, read)).ConfigureAwait(false);
                await target.FlushAsync().ConfigureAwait(false);
            }
        }

        private static IEnumerable<int> DescendantsOf(int root)
        {
            if (!Directory.Exists("/proc"))
            {
                yield break;
            }
            var parents = new Dictionary<int, List<int>>();
            foreach (var dir in Directory.EnumerateDirectories("/proc"))
            {
                if (!int.TryParse(Path.GetFileName(dir), out var pid))
                {
                    continue;
                }
                try
                {
                    var text = File.ReadAllText(Path.Combine(dir, "stat"));
                    var fields = text[(text.LastIndexOf(')') + 2)..].Split(' ');
                    if (fields[0] is "Z" or "X")
                    {
                        continue;
                    }
                    var ppid = int.Parse(fields[1], System.Globalization.CultureInfo.InvariantCulture);
                    if (!parents.TryGetValue(ppid, out var children))
                    {
                        parents[ppid] = children = new List<int>();
                    }
                    children.Add(pid);
                }
                catch (Exception ex) when (ex is IOException or FormatException or IndexOutOfRangeException or UnauthorizedAccessException)
                {
                }
            }
            var stack = new Stack<int>();
            stack.Push(root);
            var visited = new HashSet<int> { root };
            while (stack.Count > 0)
            {
                var current = stack.Pop();
                if (!parents.TryGetValue(current, out var children))
                {
                    continue;
                }
                foreach (var child in children)
                {
                    if (visited.Add(child))
                    {
                        yield return child;
                        stack.Push(child);
                    }
                }
            }
        }
    }
}
