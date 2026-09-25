using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace SportsHedge.Launcher.Core.Processes.Windows;

/// <summary>
/// Starts each child SUSPENDED, assigns it to a fresh kill-on-close Job Object,
/// then resumes it. The child therefore cannot create any descendant before it
/// is inside the job, and every descendant (venv python shim -> python,
/// cmd -> npm -> node -> next) inherits job membership.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class WindowsJobProcessLauncher : IProcessGroupLauncher
{
    // Serialises handle inheritance so no other child we create can inherit
    // another child's log handles.
    private static readonly object CreationLock = new();

    public bool ProvidesKillOnClose => true;

    public IOwnedProcessGroup Start(ProcessSpec spec)
    {
        var job = new WindowsJobObject();
        try
        {
            NativeMethods.PROCESS_INFORMATION pi;
            lock (CreationLock)
            {
                pi = CreateSuspended(spec);
            }
            try
            {
                job.Assign(pi.hProcess);
                var root = new SystemRootProcess(Process.GetProcessById(pi.dwProcessId));
                if (NativeMethods.ResumeThread(pi.hThread) == NativeMethods.RESUME_FAILED)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "ResumeThread failed");
                }
                return new JobOwnedProcessGroup(spec.Label, job, root);
            }
            catch
            {
                NativeMethods.TerminateProcess(pi.hProcess, 1);
                throw;
            }
            finally
            {
                NativeMethods.CloseHandle(pi.hThread);
                NativeMethods.CloseHandle(pi.hProcess);
            }
        }
        catch
        {
            job.Dispose();
            throw;
        }
    }

    private static NativeMethods.PROCESS_INFORMATION CreateSuspended(ProcessSpec spec)
    {
        using var stdin = OpenInheritableNul();
        using var stdout = OpenInheritableLog(spec.StdoutLog);
        using var stderr = OpenInheritableLog(spec.StderrLog);

        var startup = new NativeMethods.STARTUPINFO
        {
            cb = Marshal.SizeOf<NativeMethods.STARTUPINFO>(),
            dwFlags = NativeMethods.STARTF_USESTDHANDLES,
            hStdInput = stdin?.DangerousGetHandle() ?? IntPtr.Zero,
            hStdOutput = stdout.DangerousGetHandle(),
            hStdError = stderr.DangerousGetHandle(),
        };
        var commandLine = (WindowsCommandLine.Build(spec) + '\0').ToCharArray();
        var environment = Marshal.StringToHGlobalUni(WindowsCommandLine.EnvironmentBlock(spec.Environment));
        try
        {
            if (!NativeMethods.CreateProcessW(
                    spec.FileName,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    bInheritHandles: true,
                    NativeMethods.CREATE_SUSPENDED | NativeMethods.CREATE_UNICODE_ENVIRONMENT | NativeMethods.CREATE_NO_WINDOW,
                    environment,
                    spec.WorkingDirectory,
                    ref startup,
                    out var pi))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), $"CreateProcess failed for {spec.FileName}");
            }
            return pi;
        }
        finally
        {
            Marshal.FreeHGlobal(environment);
        }
    }

    private static SafeFileHandle OpenInheritableLog(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var handle = File.OpenHandle(path, FileMode.Create, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
        MakeInheritable(handle);
        return handle;
    }

    private static SafeFileHandle? OpenInheritableNul()
    {
        try
        {
            var handle = File.OpenHandle("NUL", FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            MakeInheritable(handle);
            return handle;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception)
        {
            return null;
        }
    }

    private static void MakeInheritable(SafeHandle handle)
    {
        if (!NativeMethods.SetHandleInformation(handle, NativeMethods.HANDLE_FLAG_INHERIT, NativeMethods.HANDLE_FLAG_INHERIT))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "SetHandleInformation failed");
        }
    }
}
