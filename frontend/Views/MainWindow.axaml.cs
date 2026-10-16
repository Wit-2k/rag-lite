// 窗口代码：选目录对话框（实现 IFolderPicker）、回车发送、聊天区自动滚到底。
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using frontend.Models;
using frontend.Services;
using frontend.ViewModels;

namespace frontend.Views;

public partial class MainWindow : Window, IFolderPicker, ISettingsDialog
{
    private MainViewModel? _viewModel;

    public MainWindow()
    {
        InitializeComponent();
        DataContextChanged += OnDataContextChanged;
    }

    private void OnDataContextChanged(object? sender, EventArgs e)
    {
        if (_viewModel is not null)
        {
            _viewModel.StreamTicked -= ScrollToEnd;
        }
        _viewModel = DataContext as MainViewModel;
        if (_viewModel is not null)
        {
            // 流式期间每次刷新正文都滚到底，跟聊天软件一样
            _viewModel.StreamTicked += ScrollToEnd;
            _viewModel.Messages.CollectionChanged += (_, _) => ScrollToEnd();
        }
    }

    private void ScrollToEnd() => Dispatcher.UIThread.Post(() => ChatScroll.ScrollToEnd(), DispatcherPriority.Background);

    /// <summary>选择 MinerU 产物目录（IFolderPicker 的实现）。</summary>
    public async Task<string?> PickFolderAsync(string title)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = title,
            AllowMultiple = false,
        });
        return folders.Count > 0 ? folders[0].TryGetLocalPath() : null;
    }

    /// <summary>弹出设置窗口（ISettingsDialog 的实现）。对话框不做 I/O，保存交给 ViewModel。</summary>
    public async Task<SettingsUpdateDto?> ShowSettingsAsync(SettingsDto current)
    {
        var window = new SettingsWindow { DataContext = new SettingsViewModel(current) };
        var saved = await window.ShowDialog<bool>(this);
        return saved ? window.Result : null;
    }

    /// <summary>回车发送；Shift+回车留给以后换行用。</summary>
    private void OnQuestionKeyDown(object? sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || e.KeyModifiers != KeyModifiers.None)
        {
            return;
        }
        e.Handled = true;
        if (_viewModel?.SendCommand.CanExecute(null) == true)
        {
            _viewModel.SendCommand.Execute(null);
        }
    }
}
