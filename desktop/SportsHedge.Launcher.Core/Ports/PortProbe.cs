using System.Diagnostics;
using System.Net;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace SportsHedge.Launcher.Core.Ports;

public sealed record PortStatus(int Port, bool Listening, int? OwnerPid = null, string? OwnerName = null)
{
    public string OwnerDescription => OwnerPid is null
        ? "an unidentified process"
        : $"PID {OwnerPid}{(string.IsNullOrWhiteSpace(OwnerName) ? string.Empty : $" ({OwnerName})")}";
}

/// <summary>Read-only port inspection. Nothing here can terminate a process.</summary>
public interface IPortProbe
{
    PortStatus Inspect(int port);
}

public sealed class SystemPortProbe : IPortProbe
{
    public PortStatus Inspect(int port)
    {
        var listening = IPGlobalProperties.GetIPGlobalProperties()
            .GetActiveTcpListeners()
            .Any(endpoint => endpoint.Port == port && IsRelevant(endpoint.Address));
        if (!listening)
        {
            return new PortStatus(port, false);
        }
        int? owner = OperatingSystem.IsWindows() ? WindowsTcpOwner.FindListenerPid(port) : null;
        string? name = null;
        if (owner is int pid)
        {
            try
            {
                using var process = Process.GetProcessById(pid);
                name = process.ProcessName;
            }
            catch (ArgumentException)
            {
            }
            catch (InvalidOperationException)
            {
            }
        }
        return new PortStatus(port, true, owner, name);
    }

    private static bool IsRelevant(IPAddress address) =>
        IPAddress.IsLoopback(address) || address.Equals(IPAddress.Any) || address.Equals(IPAddress.IPv6Any);
}

public sealed record PortRequirement(int Port, string Component);

public sealed record PreflightResult(bool Ok, string? FailedComponent, string? Message)
{
    public static readonly PreflightResult Clear = new(true, null, null);
}

public static class PortPreflight
{
    public static readonly IReadOnlyList<PortRequirement> Required = new[]
    {
        new PortRequirement(LauncherConstants.BackendPort, "backend"),
        new PortRequirement(LauncherConstants.FrontendPort, "interface"),
    };

    /// <summary>
    /// Any listener refuses startup. This controller holds the single-instance
    /// mutex, so a listener cannot be this session's own child. Nothing is killed.
    /// </summary>
    public static PreflightResult Evaluate(IPortProbe probe, IReadOnlyList<PortRequirement>? required = null)
    {
        foreach (var requirement in required ?? Required)
        {
            var status = probe.Inspect(requirement.Port);
            if (status.Listening)
            {
                return new PreflightResult(false, "Ports", FormatOccupied(status));
            }
        }
        return PreflightResult.Clear;
    }

    public static string FormatOccupied(PortStatus status) =>
        $"Sports Hedge cannot start because port {status.Port} is already in use by another process " +
        $"({status.OwnerDescription}). No processes were terminated. If the PowerShell demo launcher is running, " +
        @"stop it with scripts\windows\Stop-SportsHedge-Demo.bat, or close the other application, then try again.";
}

[SupportedOSPlatform("windows")]
internal static class WindowsTcpOwner
{
    private const int AF_INET = 2;
    private const int AF_INET6 = 23;
    private const int TCP_TABLE_OWNER_PID_LISTENER = 3;

    [DllImport("iphlpapi.dll", SetLastError = true)]
    private static extern uint GetExtendedTcpTable(IntPtr pTcpTable, ref int pdwSize, bool bOrder, int ulAf, int tableClass, uint reserved);

    public static int? FindListenerPid(int port) =>
        Find(port, AF_INET, rowSize: 24, portOffset: 8, pidOffset: 20)
        ?? Find(port, AF_INET6, rowSize: 56, portOffset: 20, pidOffset: 52);

    private static int? Find(int port, int family, int rowSize, int portOffset, int pidOffset)
    {
        var size = 0;
        GetExtendedTcpTable(IntPtr.Zero, ref size, false, family, TCP_TABLE_OWNER_PID_LISTENER, 0);
        if (size <= 0)
        {
            return null;
        }
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            if (GetExtendedTcpTable(buffer, ref size, false, family, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0)
            {
                return null;
            }
            var count = Marshal.ReadInt32(buffer);
            for (var i = 0; i < count; i++)
            {
                var row = 4 + (i * rowSize);
                var rawPort = (uint)Marshal.ReadInt32(buffer, row + portOffset);
                var localPort = (int)(((rawPort & 0xFF) << 8) | ((rawPort >> 8) & 0xFF));
                if (localPort == port)
                {
                    return Marshal.ReadInt32(buffer, row + pidOffset);
                }
            }
            return null;
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }
}
