using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using frontend.Services;
using frontend.ViewModels;
using frontend.Views;

namespace frontend;

public partial class App : Application
{
    public override void Initialize()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            // 单实例：托盘常驻之后「又双击一次」很常见，放任第二个实例跑起来会两个后端写同一个向量库。
            // 已有实例时：请它把窗口显示到前台，然后本次直接退出（界面都不建，也就不会起后端）。
            var single = SingleInstance.Acquire();
            if (!single.IsFirst)
            {
                AppLog.Write("[单实例] 已有实例在运行，请求它显示窗口后退出本次启动");
                single.SignalExisting();
                single.Dispose();
                // 不能在这里直接 Shutdown()：此时消息循环还没跑起来，关掉 Dispatcher 之后
                // StartCore 会抛「Cannot perform requested operation because the Dispatcher shut down」
                // （实测踩过）。交给循环起来之后再关。
                Dispatcher.UIThread.Post(() => desktop.Shutdown());
                base.OnFrameworkInitializationCompleted();
                return;
            }

            // 关窗不再结束程序：托盘图标还在，彻底退出走托盘菜单
            desktop.ShutdownMode = ShutdownMode.OnExplicitShutdown;

            // 后端作为子进程由界面拉起：窗口先出现（显示「正在启动后端…」），随后异步完成握手
            var backend = new BackendProcess();
            var window = new MainWindow();
            var viewModel = new MainViewModel(backend, window, window, window, window);
            window.DataContext = viewModel;
            desktop.MainWindow = window;

            // 托盘：左键点图标把窗口叫回来，右键菜单里才有「彻底退出」。
            // 挂载点只能挂到 Application 上（Avalonia 里就这一条路，挂别处会抛
            // 「TrayIcon.Icons must be set on the Application.」）；
            // 而且原生图标是在「加进集合」那一刻才创建的，所以先把 Icon/Menu/ToolTip 都设好。
            var trayIcons = new TrayIcons();
            var tray = new TrayIcon
            {
                Icon = window.Icon,            // 复用窗口图标（book.ico），不必再拼一次资源路径
                ToolTipText = "rag-lite —— 本机知识库（点图标打开窗口）",
                IsVisible = true,
            };
            var showItem = new NativeMenuItem("显示主窗口");
            showItem.Click += (_, _) => window.ShowFromTray();
            var exitItem = new NativeMenuItem("彻底退出");
            exitItem.Click += (_, _) => window.ExitApplication();
            var menu = new NativeMenu();
            menu.Add(showItem);
            menu.Add(new NativeMenuItemSeparator());
            menu.Add(exitItem);
            tray.Menu = menu;
            tray.Clicked += (_, _) => window.ShowFromTray();
            trayIcons.Add(tray);
            TrayIcon.SetIcons(this, trayIcons);

            // 别的实例请求「显示窗口」时，从后台线程切回界面线程再动窗口
            single.ListenForShowRequests(() => Dispatcher.UIThread.Post(() => window.ShowFromTray()));

            // 真正退出（托盘菜单 / 系统关机）时才收尾：结束后端进程树，别留孤儿占着端口
            desktop.Exit += (_, _) =>
            {
                single.Dispose();
                backend.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(5));
            };
            window.Opened += async (_, _) => await viewModel.InitializeAsync();

        }

        base.OnFrameworkInitializationCompleted();
    }
}
