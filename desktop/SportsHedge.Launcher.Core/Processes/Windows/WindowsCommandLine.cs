using System.Text;

namespace SportsHedge.Launcher.Core.Processes.Windows;

/// <summary>Pure CreateProcess command-line / environment-block formatting (platform-neutral for tests).</summary>
public static class WindowsCommandLine
{
    public static string Build(ProcessSpec spec)
    {
        var args = spec.RawArguments ?? string.Join(' ', spec.Arguments.Select(QuoteArgument));
        return args.Length == 0 ? QuoteArgument(spec.FileName) : $"{QuoteArgument(spec.FileName)} {args}";
    }

    /// <summary>MSVCRT / CommandLineToArgvW quoting.</summary>
    public static string QuoteArgument(string argument)
    {
        if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return argument;
        }
        var builder = new StringBuilder("\"");
        var backslashes = 0;
        foreach (var c in argument)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                builder.Append('\\', (backslashes * 2) + 1).Append('"');
            }
            else
            {
                builder.Append('\\', backslashes).Append(c);
            }
            backslashes = 0;
        }
        builder.Append('\\', backslashes * 2).Append('"');
        return builder.ToString();
    }

    /// <summary>Sorted "KEY=VALUE\0...\0" block; StringToHGlobalUni adds the final terminator.</summary>
    public static string EnvironmentBlock(IReadOnlyDictionary<string, string> environment)
    {
        var builder = new StringBuilder();
        foreach (var (key, value) in environment.OrderBy(pair => pair.Key, StringComparer.OrdinalIgnoreCase))
        {
            if (key.Length == 0 || (key[0] != '=' && key.Contains('=', StringComparison.Ordinal)))
            {
                continue;
            }
            builder.Append(key).Append('=').Append(value).Append('\0');
        }
        if (builder.Length == 0)
        {
            builder.Append('\0');
        }
        return builder.ToString();
    }
}
