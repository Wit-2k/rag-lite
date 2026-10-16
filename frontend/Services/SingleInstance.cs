// 单实例保障：托盘常驻之后，「又双击了一次 RagLite.exe」会变成很常见的事。
// 放任第二个实例跑起来，就会出现两个后端同时读写同一个向量库目录——正是要避免的情形。
//
// 做法全是托管 API，不需要窗口标题/类名那套 FindWindow：
//  ① 命名互斥体判断「我是不是第一个」；
//  ② 命名事件（EventWaitHandle）用于「第二个实例请第一个实例把窗口显示到前台」；
//  ③ 唯一一处原生调用 AllowSetForegroundWindow(ASFW_ANY)：让被请求的那个进程
//     有资格抢前台（Windows 默认只允许当前前台进程抢，刚被用户双击的那个进程才有这个资格）。
using System.Runtime.InteropServices;

namespace frontend.Services;

/// <summary>同一时间只允许一个实例；已有实例时负责把它唤到前台。</summary>
internal sealed class SingleInstance : IDisposable
{
    private const string MutexName = @"Local\rag-lite-single-instance";
    private const string ShowEventName = @"Local\rag-lite-show-window";
    private const int AsfwAny = -1; // AllowSetForegroundWindow 的「任意进程」常量

    private Mutex? _mutex;
    private EventWaitHandle? _showEvent;
    private Thread? _listener;

    private SingleInstance(bool isFirst, Mutex mutex, EventWaitHandle showEvent)
    {
        IsFirst = isFirst;
        _mutex = mutex;
        _showEvent = showEvent;
    }

    /// <summary>true = 本次是第一个实例（正常继续启动）；false = 已有实例在跑。</summary>
    public bool IsFirst { get; }

    /// <summary>抢一个单实例名额。尽量不做任何界面初始化就能判断出来。</summary>
    public static SingleInstance Acquire()
    {
        var mutex = new Mutex(initiallyOwned: true, MutexName, out var createdNew);
        var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName);
        return new SingleInstance(createdNew, mutex, showEvent);
    }

    /// <summary>非第一个实例时调用：请求已有实例把窗口显示到前台，然后自己退出。</summary>
    public void SignalExisting()
    {
        try
        {
            AllowSetForegroundWindow(AsfwAny); // 先把自己的「抢前台资格」转交出去
            _showEvent?.Set();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "通知已有实例显示窗口");
        }
    }

    /// <summary>第一个实例调用：后台等「请显示窗口」的请求，收到就在界面上执行 onShow。</summary>
    public void ListenForShowRequests(Action onShow)
    {
        _listener = new Thread(() =>
        {
            while (true)
            {
                try
                {
                    if (_showEvent is null || !_showEvent.WaitOne())
                    {
                        return;
                    }
                }
                catch (Exception ex)
                {
                    AppLog.Error(ex, "等待显示窗口的请求");
                    return;
                }
                AppLog.Write("[单实例] 收到「显示窗口」请求");
                onShow();
            }
        })
        {
            IsBackground = true, // 主进程退出时它不该拖着不让走
            Name = "single-instance-listener",
        };
        _listener.Start();
    }

    public void Dispose()
    {
        try
        {
            _showEvent?.Close();
            _showEvent = null;
            _mutex?.ReleaseMutex();
        }
        catch (Exception)
        {
            // 互斥体本来就不属于本线程时 ReleaseMutex 会抛，忽略即可（进程退出时系统会释放）
        }
        _mutex?.Dispose();
        _mutex = null;
    }

    [DllImport("user32.dll")]
    private static extern bool AllowSetForegroundWindow(int processId);
}
