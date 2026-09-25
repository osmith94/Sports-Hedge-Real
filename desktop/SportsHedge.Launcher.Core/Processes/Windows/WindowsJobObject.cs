using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace SportsHedge.Launcher.Core.Processes.Windows;

/// <summary>
/// Anonymous Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The controller
/// holds the only handle; when it is closed (normal dispose, controller crash,
/// or Task Manager "End task" on SportsHedge.exe) Windows terminates every
/// process in the job. Breakaway is not permitted, so descendants stay inside.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class WindowsJobObject : IJobObject
{
    private readonly SafeJobHandle _handle;

    public WindowsJobObject()
    {
        _handle = NativeMethods.CreateJobObjectW(IntPtr.Zero, null);
        if (_handle.IsInvalid)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
        }
        var info = new NativeMethods.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags =
            NativeMethods.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | NativeMethods.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
        if (!NativeMethods.SetInformationJobObject(
                _handle,
                NativeMethods.JobObjectInfoClass.ExtendedLimitInformation,
                ref info,
                (uint)Marshal.SizeOf<NativeMethods.JOBOBJECT_EXTENDED_LIMIT_INFORMATION>()))
        {
            var error = Marshal.GetLastWin32Error();
            _handle.Dispose();
            throw new Win32Exception(error, "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed");
        }
    }

    public bool KillOnClose
    {
        get
        {
            var info = new NativeMethods.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            if (!NativeMethods.QueryInformationJobObject(
                    _handle,
                    NativeMethods.JobObjectInfoClass.ExtendedLimitInformation,
                    ref info,
                    (uint)Marshal.SizeOf<NativeMethods.JOBOBJECT_EXTENDED_LIMIT_INFORMATION>(),
                    out _))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "QueryInformationJobObject failed");
            }
            return (info.BasicLimitInformation.LimitFlags & NativeMethods.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) != 0;
        }
    }

    internal void Assign(IntPtr processHandle)
    {
        if (!NativeMethods.AssignProcessToJobObject(_handle, processHandle))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
        }
    }

    public IReadOnlyList<int> ProcessIds()
    {
        if (_handle.IsClosed)
        {
            return Array.Empty<int>();
        }
        var capacity = 64;
        while (true)
        {
            // JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORD counters, then ULONG_PTR[]
            // (offset 8 on both x86 and x64).
            const int headerSize = 8;
            var size = headerSize + (capacity * IntPtr.Size);
            var buffer = Marshal.AllocHGlobal(size);
            try
            {
                if (!NativeMethods.QueryInformationJobObject(
                        _handle, NativeMethods.JobObjectInfoClass.BasicProcessIdList, buffer, (uint)size, out _))
                {
                    var error = Marshal.GetLastWin32Error();
                    const int ERROR_MORE_DATA = 234;
                    if (error == ERROR_MORE_DATA && capacity < 65536)
                    {
                        capacity *= 4;
                        continue;
                    }
                    throw new Win32Exception(error, "QueryInformationJobObject(BasicProcessIdList) failed");
                }
                var assigned = Marshal.ReadInt32(buffer, 0);
                var inList = Marshal.ReadInt32(buffer, 4);
                if (assigned > inList && capacity < 65536)
                {
                    capacity *= 4;
                    continue;
                }
                var ids = new int[inList];
                for (var i = 0; i < inList; i++)
                {
                    ids[i] = (int)Marshal.ReadIntPtr(buffer, headerSize + (i * IntPtr.Size)).ToInt64();
                }
                return ids;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }
    }

    public void Terminate(uint exitCode)
    {
        if (_handle.IsClosed)
        {
            return;
        }
        if (!NativeMethods.TerminateJobObject(_handle, exitCode))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "TerminateJobObject failed");
        }
    }

    public void Dispose() => _handle.Dispose();
}
