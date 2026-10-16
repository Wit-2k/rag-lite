using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
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
            // 后端作为子进程由界面拉起：窗口先出现（显示「正在启动后端…」），随后异步完成握手
            var backend = new BackendProcess();
            var window = new MainWindow();
            var viewModel = new MainViewModel(backend, window, window);
            window.DataContext = viewModel;
            desktop.MainWindow = window;

            // 关窗即结束后端，别留孤儿进程占着端口
            desktop.Exit += (_, _) => backend.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(5));
            window.Opened += async (_, _) => await viewModel.InitializeAsync();
        }

        base.OnFrameworkInitializationCompleted();
    }
}
