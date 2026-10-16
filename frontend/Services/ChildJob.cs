// 用 Windows 的「作业对象（Job Object）」绑住后端进程树：
// 作业句柄一关，作业里的所有进程会被系统直接杀掉——界面进程无论怎么死（任务管理器强杀、
// 崩溃、断电前的强杀），句柄都随之关闭，后端就不会变成孤儿常驻占用端口和向量库目录。
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace frontend.Services;

/// <summary>把子进程树关进一个「界面一死就全杀」的作业对象。建不出来就算了（退化成旧行为）。</summary>
internal sealed class ChildJob : IDisposable
{
    private const int ExtendedLimitInformationClass = 9;  // JobObjectExtendedLimitInformation 的信息类别号
    private const uint JobObjectLimitKillOnJobClose = 0x2000;

    private IntPtr _handle;

    private ChildJob(IntPtr handle)
    {
        _handle = handle;
    }

    /// <summary>建一个「句柄关闭即杀光成员」的作业；系统不给建就返回 null（不致命，日志里会说明）。</summary>
    public static ChildJob? TryCreate()
    {
        var handle = CreateJobObjectW(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
        {
            AppLog.Write($"[后端] 提示：CreateJobObject 失败（错误码 {Marshal.GetLastWin32Error()}），改用看门狗兜底");
            return null;
        }

        var limits = new JobObjectExtendedLimitInformation
        {
            BasicLimitInformation = new JobObjectBasicLimitInformation
            {
                LimitFlags = JobObjectLimitKillOnJobClose,
            },
        };
        var size = Marshal.SizeOf<JobObjectExtendedLimitInformation>();
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(limits, buffer, fDeleteOld: false);
            if (!SetInformationJobObject(handle, ExtendedLimitInformationClass, buffer, (uint)size))
            {
                AppLog.Write($"[后端] 提示：SetInformationJobObject 失败（错误码 {Marshal.GetLastWin32Error()}），改用看门狗兜底");
                CloseHandle(handle);
                return null;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
        return new ChildJob(handle);
    }

    /// <summary>把刚起的后端进程放进作业。之后它自己再起的子进程（比如 venv 的 python 转发进程）也会自动落在同一个作业里。</summary>
    public bool Assign(Process process)
    {
        if (_handle == IntPtr.Zero)
        {
            return false;
        }
        try
        {
            return AssignProcessToJobObject(_handle, process.Handle);
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "把后端放进作业对象");
            return false;
        }
    }

    public void Dispose()
    {
        if (_handle != IntPtr.Zero)
        {
            // 只关句柄就够：KILL_ON_JOB_CLOSE 会让系统把成员进程一并结束
            CloseHandle(_handle);
            _handle = IntPtr.Zero;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObjectW(IntPtr attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
}
