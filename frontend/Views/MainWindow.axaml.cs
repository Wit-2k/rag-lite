// 窗口代码：选目录对话框（实现 IFolderPicker）、回车发送、聊天区自动滚到底，
// 以及「关窗只收进托盘、彻底退出走托盘菜单」的收尾逻辑。
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Input;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using frontend.Models;
using frontend.Services;
using frontend.ViewModels;

namespace frontend.Views;

public partial class MainWindow : Window, IFolderPicker, ISettingsDialog, IGroupsDialog
{
    private MainViewModel? _viewModel;

    /// <summary>true 表示这次关闭是真的要退出（托盘菜单「彻底退出」会先把它置上）。</summary>
    private bool _exiting;

    public MainWindow()
    {
        InitializeComponent();
        DataContextChanged += OnDataContextChanged;
        Closing += OnClosing;
    }

    /// <summary>点关闭键只是收进托盘：不结束进程，后端继续跑（导入、对话都还在）。
    /// 真正退出走托盘的「彻底退出」，那条路会先把 _exiting 置上。</summary>
    private void OnClosing(object? sender, WindowClosingEventArgs e)
    {
        if (_exiting)
        {
            return;
        }
        e.Cancel = true;
        Hide();
        AppLog.Write("[托盘] 窗口已收进托盘（进程与后端继续运行）");
        _viewModel?.NotifyHiddenToTray();
    }

    /// <summary>从托盘把窗口叫回来（左键点托盘图标、或托盘菜单里的「显示主窗口」）。</summary>
    public void ShowFromTray()
    {
        Show();
        if (WindowState == WindowState.Minimized)
        {
            WindowState = WindowState.Normal;
        }
        Activate();
        // Windows 有时不让后台进程直接抢前台，用「置顶再取消」轻推一下
        if (!Topmost)
        {
            Topmost = true;
            Topmost = false;
        }
    }

    /// <summary>彻底退出：先允许本次关闭，再让应用退出——App 的 Exit 回调会连同后端一起收尾。</summary>
    public void ExitApplication()
    {
        AppLog.Write("[托盘] 请求彻底退出");
        _exiting = true;
        if (Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.Shutdown();
        }
        else
        {
            Close();
        }
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

    /// <summary>选择 MinerU 产物目录，支持一次多选（IFolderPicker 的实现）。</summary>
    public async Task<IReadOnlyList<string>> PickFoldersAsync(string title)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = title,
            AllowMultiple = true,
        });
        return folders
            .Select(folder => folder.TryGetLocalPath())
            .Where(path => !string.IsNullOrEmpty(path))
            .Select(path => path!)
            .ToList();
    }

    /// <summary>弹出设置窗口（ISettingsDialog 的实现）。对话框不做 I/O，保存交给 ViewModel。</summary>
    public async Task<SettingsUpdateDto?> ShowSettingsAsync(SettingsDto current)
    {
        var window = new SettingsWindow { DataContext = new SettingsViewModel(current) };
        var saved = await window.ShowDialog<bool>(this);
        return saved ? window.Result : null;
    }

    /// <summary>弹出分组管理窗口（IGroupsDialog 的实现）。改动由窗口内的 ViewModel 直接落库，
    /// 每改一次就回调 onChanged，让主界面把分组、数据源、会话三处重新对齐。</summary>
    public async Task ShowDialogAsync(ApiClient api, Func<Task> onChanged)
    {
        var window = new GroupsWindow { DataContext = new GroupsViewModel(api, onChanged) };
        await window.ShowDialog(this);
    }

    /// <summary>双击会话标题进入重命名。</summary>
    private void OnSessionTitleDoubleTapped(object? sender, Avalonia.Input.TappedEventArgs e)
    {
        if (sender is Control { DataContext: SessionItemViewModel item } && _viewModel is not null)
        {
            e.Handled = true;
            if (_viewModel.BeginRenameCommand.CanExecute(item))
            {
                _viewModel.BeginRenameCommand.Execute(item);
            }
        }
    }

    /// <summary>会话重命名输入框出现时自动聚焦（让用户可以直接开始打字）。</summary>
    private void OnRenameBoxAttached(object? sender, Avalonia.VisualTreeAttachmentEventArgs e)
    {
        if (sender is TextBox box)
        {
            Dispatcher.UIThread.Post(() => box.Focus(), DispatcherPriority.Background);
        }
    }

    /// <summary>重命名输入框：回车提交、Esc 放弃。</summary>
    private void OnRenameKeyDown(object? sender, KeyEventArgs e)
    {
        if (sender is not TextBox box || box.Tag is not SessionItemViewModel item)
        {
            return;
        }
        if (_viewModel is null)
        {
            return;
        }
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            if (_viewModel.CommitRenameCommand.CanExecute(item))
            {
                _viewModel.CommitRenameCommand.Execute(item);
            }
        }
        else if (e.Key == Key.Escape)
        {
            e.Handled = true;
            if (_viewModel.CancelRenameCommand.CanExecute(item))
            {
                _viewModel.CancelRenameCommand.Execute(item);
            }
        }
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
