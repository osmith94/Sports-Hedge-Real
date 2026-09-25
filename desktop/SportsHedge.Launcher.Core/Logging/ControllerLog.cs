using System.Text;

namespace SportsHedge.Launcher.Core.Logging;

public interface ILog
{
    void Info(string message);
    void Warn(string message);
    void Error(string message);
}

/// <summary>Replaces registered secret values with a fixed marker in every log line.</summary>
public sealed class SecretRedactor
{
    public const string Marker = "[REDACTED]";
    private readonly object _gate = new();
    private readonly List<string> _secrets = new();

    public void Register(string secret)
    {
        if (string.IsNullOrEmpty(secret))
        {
            return;
        }
        lock (_gate)
        {
            _secrets.Add(secret);
        }
    }

    public string Redact(string text)
    {
        lock (_gate)
        {
            foreach (var secret in _secrets)
            {
                text = text.Replace(secret, Marker, StringComparison.Ordinal);
            }
        }
        return text;
    }
}

/// <summary>Append-only, thread-safe controller log (logs\desktop-controller.log).</summary>
public sealed class FileControllerLog : ILog
{
    private readonly object _gate = new();
    private readonly string _path;
    private readonly SecretRedactor _redactor;
    private readonly Action<string>? _mirror;

    public FileControllerLog(string path, SecretRedactor redactor, Action<string>? mirror = null)
    {
        _path = path;
        _redactor = redactor;
        _mirror = mirror;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
    }

    public void Info(string message) => Write("INFO", message);
    public void Warn(string message) => Write("WARN", message);
    public void Error(string message) => Write("ERROR", message);

    private void Write(string level, string message)
    {
        var line = $"{DateTimeOffset.UtcNow:yyyy-MM-ddTHH:mm:ss.fffZ} {level} {_redactor.Redact(message)}";
        lock (_gate)
        {
            try
            {
                File.AppendAllText(_path, line + Environment.NewLine, Encoding.UTF8);
            }
            catch (IOException)
            {
                // Logging must never take the controller down.
            }
        }
        _mirror?.Invoke(line);
    }
}

public static class LogFiles
{
    /// <summary>Keep exactly one previous session's copy of a child log.</summary>
    public static void RollToPrevious(string path)
    {
        try
        {
            if (!File.Exists(path))
            {
                return;
            }
            var previous = Path.Combine(
                Path.GetDirectoryName(path)!,
                Path.GetFileNameWithoutExtension(path) + ".previous" + Path.GetExtension(path));
            File.Copy(path, previous, overwrite: true);
            File.WriteAllText(path, string.Empty);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    public static string Tail(string path, int maxLines = 12)
    {
        try
        {
            if (!File.Exists(path))
            {
                return string.Empty;
            }
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream);
            var lines = new Queue<string>();
            string? line;
            while ((line = reader.ReadLine()) is not null)
            {
                lines.Enqueue(line);
                if (lines.Count > maxLines)
                {
                    lines.Dequeue();
                }
            }
            return string.Join(Environment.NewLine, lines);
        }
        catch (IOException)
        {
            return string.Empty;
        }
    }
}
