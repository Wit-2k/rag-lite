using Avalonia;
using System;
using frontend.Services;

namespace frontend;

sealed class Program
{
    // Initialization code. Don't use any Avalonia, third-party APIs or any
    // SynchronizationContext-reliant code before AppMain is called: things aren't initialized
    // yet and stuff might break.
    [STAThread]
    public static void Main(string[] args)
    {
        // 界面程序没有控制台，未处理异常统一落到日志文件，出问题时能查
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            AppLog.Write($"[致命] {e.ExceptionObject}");
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            AppLog.Write($"[未观察的任务异常] {e.Exception}");
            e.SetObserved();
        };
        AppLog.Write("=== 客户端启动 ===");
        try
        {
            BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
        }
        catch (Exception ex)
        {
            // 原生库/初始化阶段的崩溃不一定走 AppDomain.UnhandledException，这里兜住并落盘
            AppLog.Write($"[致命] 界面启动失败：{ex}");
            throw;
        }
    }

    // Avalonia configuration, don't remove; also used by visual designer.
    public static AppBuilder BuildAvaloniaApp()
        => AppBuilder.Configure<App>()
            .UsePlatformDetect()
#if DEBUG
            .WithDeveloperTools()
#endif
            .WithInterFont()
            .LogToTrace();
}
