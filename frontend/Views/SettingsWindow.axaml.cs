// 设置窗口：只负责收集编辑结果，不发网络请求（保存由 MainViewModel 调 ApiClient 完成）。
using Avalonia.Controls;
using frontend.Models;
using frontend.ViewModels;

namespace frontend.Views;

public partial class SettingsWindow : Window
{
    public SettingsWindow()
    {
        InitializeComponent();
    }

    /// <summary>用户点「保存并生效」且校验通过时的结果；取消则为 null。</summary>
    public SettingsUpdateDto? Result { get; private set; }

    private void OnSave(object? sender, Avalonia.Interactivity.RoutedEventArgs e)
    {
        if (DataContext is not SettingsViewModel viewModel)
        {
            return;
        }
        var update = viewModel.TryBuildUpdate();
        if (update is null)
        {
            return; // 校验失败：错误信息已显示在窗口底部
        }
        Result = update;
        Close(true);
    }

    private void OnCancel(object? sender, Avalonia.Interactivity.RoutedEventArgs e)
    {
        Result = null;
        Close(false);
    }
}
