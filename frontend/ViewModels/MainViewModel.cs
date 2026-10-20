// 主界面：左边管数据源（添加目录、看导入进度），右边对话（流式回答 + 清空上下文）。
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Text;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FluentAvalonia.UI.Controls;
using frontend.Models;
using frontend.Services;

namespace frontend.ViewModels;

public partial class MainViewModel : ObservableObject
{
    // 流式回答的刷新节奏：攒够 12 个字或过了 100ms 才写回界面，
    // 免得每来一个 token 就重新解析一次 Markdown（长回答会明显掉帧）。
    private const int FlushChars = 12;
    private static readonly TimeSpan FlushInterval = TimeSpan.FromMilliseconds(100);

    private readonly BackendProcess _backend;
    private readonly IFolderPicker _picker;
    private readonly ISettingsDialog _settingsDialog;
    private readonly IGroupsDialog _groupsDialog;
    private readonly StringBuilder _pending = new();
    private readonly Stopwatch _sinceFlush = Stopwatch.StartNew();

    private ApiClient? _api;
    private MessageViewModel? _streaming;
    private CancellationTokenSource? _chatCts;
    private int _flushedLength;
    private bool _trayHintShown;
    private bool _switchingSession;   // 切换会话时装载消息，避免与列表刷新互相触发

    public MainViewModel(
        BackendProcess backend, IFolderPicker picker, ISettingsDialog settingsDialog, IGroupsDialog groupsDialog)
    {
        _backend = backend;
        _picker = picker;
        _settingsDialog = settingsDialog;
        _groupsDialog = groupsDialog;
    }

    [ObservableProperty]
    public partial string Status { get; set; } = "正在启动后端…";

    [ObservableProperty]
    public partial bool IsReady { get; set; }

    /// <summary>配置提示：密钥没配、有数据源需要重导时显示在对话区顶部。</summary>
    [ObservableProperty]
    public partial string ConfigHint { get; set; } = "";

    /// <summary>配置提示的严重级别：有缺失/过期项用 Warning，正常状态用 Informational。</summary>
    [ObservableProperty]
    public partial FAInfoBarSeverity ConfigSeverity { get; set; } = FAInfoBarSeverity.Informational;

    [ObservableProperty]
    public partial string Question { get; set; } = "";

    [ObservableProperty]
    public partial bool IsAsking { get; set; }

    [ObservableProperty]
    public partial HealthDto? Health { get; set; }

    public ObservableCollection<SourceItemViewModel> Sources { get; } = [];

    /// <summary>左栏实际显示的数据源（= Sources 按分组筛选后的结果）。
    /// 列表绑定的是它，筛掉的行走的是「从集合里拿掉」，不会留在布局里占空位。</summary>
    public ObservableCollection<SourceItemViewModel> SourcesView { get; } = [];

    /// <summary>所有数据源分组（后端按名字排，带成员数）。</summary>
    public ObservableCollection<GroupDto> Groups { get; } = [];

    /// <summary>左栏顶部「分组」筛选下拉的选项。</summary>
    public ObservableCollection<GroupScopeOption> GroupFilters { get; } = [];

    /// <summary>当前筛选条件（默认「全部数据源」）。只影响左栏显示什么，不影响检索。</summary>
    [ObservableProperty]
    public partial GroupScopeOption? SelectedGroupFilter { get; set; }

    /// <summary>对话区「检索范围」下拉的选项（绑定当前会话）。</summary>
    public ObservableCollection<GroupScopeOption> ScopeOptions { get; } = [];

    /// <summary>当前会话的检索范围（切换会话时会跟着变）。</summary>
    [ObservableProperty]
    public partial GroupScopeOption? SelectedScope { get; set; }

    /// <summary>当前检索范围为空分组时的提醒（例如「该分组里还没有数据源」）。</summary>
    [ObservableProperty]
    public partial string ScopeWarning { get; set; } = "";

    /// <summary>筛选后一个数据源都没显示时的提示。</summary>
    [ObservableProperty]
    public partial string SourceFilterHint { get; set; } = "";

    private bool _syncingScope;   // 程序化改下拉选中项时，别当成用户操作去调接口

    /// <summary>会话列表（后端按「最近使用」倒序返回）。</summary>
    public ObservableCollection<SessionItemViewModel> Sessions { get; } = [];

    /// <summary>当前会话；为 null 表示还没选（启动时会自动建一个）。</summary>
    [ObservableProperty]
    public partial SessionItemViewModel? CurrentSession { get; set; }

    public ObservableCollection<MessageViewModel> Messages { get; } = [];

    /// <summary>流式过程中每次刷新正文都会触发，界面据此把聊天区滚到底。</summary>
    public event Action? StreamTicked;

    /// <summary>窗口打开后调用：起后端 → 建客户端 → 拉一次状态。</summary>
    public async Task InitializeAsync()
    {
        try
        {
            var baseAddress = await _backend.StartAsync();
            _api = new ApiClient(baseAddress);
            await RefreshAsync();
            await EnsureSessionsAsync();   // 会话是聊天的前提：没有就建一个，用户打开就能问
            if (Messages.Count == 0)
            {
                Messages.Add(new MessageViewModel(
                    isUser: false,
                    content: "后端已就绪。左侧添加 MinerU 产物目录即可建立知识库，然后在下面提问。"));
            }
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "初始化");
            Status = $"后端启动失败：{ex.Message}";
            Messages.Add(new MessageViewModel(isUser: false, content: $"⚠️ 后端启动失败：{ex.Message}\n\n启动命令：{_backend.CommandLine}"));
        }
    }

    /// <summary>窗口被收进托盘时调用：状态栏写一行；第一次再往对话区留一句，免得以为程序关了。</summary>
    public void NotifyHiddenToTray()
    {
        Status = "已收进托盘（右键托盘图标可彻底退出）";
        if (_trayHintShown)
        {
            return;
        }
        _trayHintShown = true;
        Messages.Add(new MessageViewModel(
            isUser: false,
            content: "程序已收进系统托盘，后端继续运行。\n\n"
                     + "· 点托盘图标（或再双击一次 RagLite.exe）可以把窗口叫回来\n"
                     + "· 要彻底退出（连后端一起关掉），右键托盘图标选「彻底退出」"));
    }

    /// <summary>重新读一次健康状态与数据源列表。</summary>
    [RelayCommand]
    private async Task RefreshAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            Health = await _api.GetHealthAsync();
            await RefreshGroupsAsync();
            var rows = await _api.ListSourcesAsync();
            MergeSources(rows);
            await RefreshSessionsAsync();
            IsReady = true;
            ConfigHint = BuildHint(Health);
            ConfigSeverity = !Health.Configured || Health.SourcesStale > 0
                ? FAInfoBarSeverity.Warning
                : FAInfoBarSeverity.Informational;
            Status = $"库内 {Health.Chunks} 块 / {Health.Sources} 个数据源";
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "刷新状态");
            Status = $"刷新失败：{ex.Message}";
        }
    }

    private static string BuildHint(HealthDto health)
    {
        var hints = new List<string>();
        if (!health.Configured)
        {
            // 路径不能用写死的 backend/.env：便携版的 .env 在便携根，源码运行才在 backend/（由后端告知）
            hints.Add($"还没有配密钥（缺 {string.Join("、", health.MissingKeys)}）："
                      + $"点右上角「设置」填，或写进 {health.EnvFile}——写完会自动生效，不用重启");
        }
        if (health.SourcesStale > 0)
        {
            hints.Add($"有 {health.SourcesStale} 个数据源是用别的嵌入模型入库的，建议在左侧重新导入");
        }
        return hints.Count == 0
            ? $"回答 {health.ChatModel.Split('/')[^1]} · 嵌入 {health.EmbedDim} 维"
            : string.Join("；", hints);
    }

    /// <summary>用新数据刷新列表：已有行原地更新，新的追加，消失的移除。</summary>
    private void MergeSources(IReadOnlyList<SourceDto> rows)
    {
        foreach (var row in rows)
        {
            var existing = Sources.FirstOrDefault(item => item.Dto.SourceId == row.SourceId);
            if (existing is null)
            {
                Sources.Add(new SourceItemViewModel(row));
            }
            else
            {
                existing.Update(row);
            }
        }
        // 只清理「后端列表里已经没有」的行。pending: 开头的是正在导入的占位行，
        // 它要等导入成功后才会换成真正的数据源 id（见 WatchJobAsync），所以不能在这里删掉——
        // 多个目录并发导入时，任何一行的刷新都会走到这里。
        foreach (var stale in Sources.Where(item =>
                     !item.Dto.SourceId.StartsWith("pending:", StringComparison.Ordinal)
                     && rows.All(row => row.SourceId != item.Dto.SourceId)).ToList())
        {
            Sources.Remove(stale);
        }
        SyncSourceGroups();
    }

    // ---------------------------------------------------------------- 分组

    /// <summary>重读分组列表，并把两个下拉（左栏筛选、对话区检索范围）对齐过去。</summary>
    private async Task RefreshGroupsAsync()
    {
        if (_api is null)
        {
            return;
        }
        var rows = await _api.ListGroupsAsync();
        Groups.Clear();
        foreach (var row in rows)
        {
            Groups.Add(row);
        }
        RebuildScopeOptions();
        SyncSourceGroups();
    }

    /// <summary>用最新的分组列表重建两个下拉的选项，尽量保住用户当前选的那一项。</summary>
    private void RebuildScopeOptions()
    {
        // 记住用户原来选的是哪一项（种类 + 分组 id），重建后尽量选回同一项
        var previous = SelectedGroupFilter;
        GroupFilters.Clear();
        foreach (var option in GroupScopeOption.Build(Groups, includeUngrouped: true))
        {
            GroupFilters.Add(option);
        }
        SelectedGroupFilter = GroupFilters.FirstOrDefault(option =>
                                option.Kind == (previous?.Kind ?? GroupScopeKind.All)
                                && option.GroupId == previous?.GroupId)
                            ?? GroupFilters[0];

        var scopeId = SelectedScope?.GroupId;
        ScopeOptions.Clear();
        foreach (var option in GroupScopeOption.Build(Groups, includeUngrouped: false))
        {
            ScopeOptions.Add(option);
        }
        var current = CurrentSession?.Dto.GroupId;
        var matching = ScopeOptions.FirstOrDefault(option => option.GroupId == current) ?? ScopeOptions[0];
        _syncingScope = true;
        try
        {
            SelectedScope = matching;
        }
        finally
        {
            _syncingScope = false;
        }
        UpdateScopeWarning();
    }

    /// <summary>把每行的只读「分组：…」摘要对齐到最新的分组集合。</summary>
    private void SyncSourceGroups()
    {
        foreach (var item in Sources)
        {
            item.SyncGroups(Groups);
        }
        ApplyGroupFilter();
    }

    /// <summary>左栏按分组筛选：只改显示，不动数据（也不重建每个卡片，避免闪烁）。</summary>
    private void ApplyGroupFilter()
    {
        var filter = SelectedGroupFilter ?? GroupFilters.FirstOrDefault();
        // 正在导入的占位行始终显示（用户得看得见进度）
        var wanted = Sources
            .Where(item => item.Dto.SourceId.StartsWith("pending:", StringComparison.Ordinal)
                           || filter is null
                           || filter.Matches(item.Dto))
            .ToList();

        // 导入期间每 500ms 刷新一次：可见集合没变就别动，免得列表反复重建
        if (!wanted.SequenceEqual(SourcesView))
        {
            SourcesView.Clear();
            foreach (var item in wanted)
            {
                SourcesView.Add(item);
            }
        }
        SourceFilterHint = wanted.Count == 0 ? "这个筛选条件下没有数据源" : "";
    }

    partial void OnSelectedGroupFilterChanged(GroupScopeOption? value) => ApplyGroupFilter();

    /// <summary>会话的检索范围变了（下拉里选的）：存到后端，并把这一行的备注改过来。</summary>
    partial void OnSelectedScopeChanged(GroupScopeOption? value)
    {
        if (_syncingScope || value is null || CurrentSession is null)
        {
            return;
        }
        var session = CurrentSession;
        var groupId = value.Kind == GroupScopeKind.Group ? value.GroupId : null;
        if (session.Dto.GroupId == groupId)
        {
            return;
        }
        _ = SaveScopeAsync(session, groupId);
    }

    private async Task SaveScopeAsync(SessionItemViewModel session, string? groupId)
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            var updated = await _api.SetSessionGroupAsync(session.SessionId, groupId);
            session.Update(updated);
            ApplyGroupNames();
            // 换范围只影响之后的问题，所以这里不重载消息，只提示一句
            Status = groupId is null
                ? $"会话「{session.Title}」的检索范围：全部数据源"
                : $"会话「{session.Title}」的检索范围：{Groups.FirstOrDefault(g => g.GroupId == groupId)?.Name}";
            UpdateScopeWarning();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "设置检索范围");
            Status = $"设置检索范围失败：{ex.Message}";
        }
    }

    /// <summary>当前范围对应的分组里一个数据源都没有时，给一句提醒（否则问了会答「没有依据」）。</summary>
    private void UpdateScopeWarning()
    {
        var groupId = SelectedScope?.GroupId;
        if (groupId is null)
        {
            ScopeWarning = "";
            return;
        }
        var count = Groups.FirstOrDefault(group => group.GroupId == groupId)?.SourceCount ?? 0;
        ScopeWarning = count == 0 ? "⚠ 这个分组里还没有数据源，提问将检索不到任何片段" : "";
    }

    /// <summary>给会话行补上「它绑的是哪个分组」的名字。</summary>
    private void ApplyGroupNames()
    {
        foreach (var session in Sessions)
        {
            var groupId = session.Dto.GroupId;
            session.GroupName = groupId is null
                ? ""
                : Groups.FirstOrDefault(group => group.GroupId == groupId)?.Name ?? "（已删除的分组）";
        }
    }

    /// <summary>打开「分组管理」对话框（新建 / 改名 / 删除，以及在组内添加或移出数据源）。</summary>
    [RelayCommand]
    private async Task ManageGroupsAsync()
    {
        if (_api is null)
        {
            return;
        }
        await _groupsDialog.ShowDialogAsync(_api, RefreshAllAfterGroupsAsync);
    }

    /// <summary>分组管理里动过之后：分组、数据源、会话三处都要重新对齐。</summary>
    private async Task RefreshAllAfterGroupsAsync()
    {
        await RefreshGroupsAsync();
        var rows = await _api!.ListSourcesAsync();
        MergeSources(rows);
        ApplyGroupNames();
        UpdateScopeWarning();
    }

    /// <summary>选若干 MinerU 产物目录并导入：每个目录一个后台任务，一起并发跑。
    /// 后端同时最多跑 MAX_PARALLEL_IMPORTS 个（默认 3），多出来的排队。</summary>
    [RelayCommand]
    private async Task AddSourceAsync()
    {
        if (_api is null || IsAsking)
        {
            return;
        }
        var paths = await _picker.PickFoldersAsync("选择 MinerU 产物目录（可多选，里面要有 full.md）");
        if (paths.Count == 0)
        {
            return;
        }

        var jobs = new List<(string JobId, SourceItemViewModel Item)>();
        var rejected = new List<string>();
        foreach (var path in paths)
        {
            try
            {
                var jobId = await _api.AddSourceAsync(path);
                // 先在列表里放一行占位（id 用 pending:jobId），后端一返回 job_id 就能看见「排队中」
                var item = new SourceItemViewModel(new SourceDto(
                    SourceId: $"pending:{jobId}", Name: Path.GetFileName(path), Path: path, ChunkCount: 0,
                    ImageCount: 0, AddedAt: "", EmbeddedAt: "", Missing: false, EmbedModel: "", EmbedDim: 0,
                    Stale: false, GroupIds: []))
                {
                    IsImporting = true,
                    Status = "排队中…",
                };
                Sources.Add(item);
                jobs.Add((jobId, item));
            }
            catch (Exception ex)
            {
                // 单个目录被拒（比如已经在导入队列里、或者没有 full.md）不影响其余目录
                AppLog.Error(ex, "添加数据源");
                rejected.Add($"{Path.GetFileName(path)}：{ex.Message}");
            }
        }

        if (jobs.Count == 0)
        {
            Status = $"没能添加：{string.Join("；", rejected)}";
            return;
        }

        Status = jobs.Count == 1 ? "正在导入…" : $"正在导入 {jobs.Count} 个目录…";
        // 每个任务各跑一个轮询循环，彼此不阻塞：任务在排队时，它们就是一起等
        var finished = await Task.WhenAll(jobs.Select(job => WatchJobAsync(job.JobId, job.Item)));
        await RefreshAsync();

        var ok = finished.Count(done => done);
        Status = ok == finished.Length
            ? $"导入完成：{ok} 个目录"
            : $"导入结束：成功 {ok} 个，失败 {finished.Length - ok} 个";
        if (rejected.Count > 0)
        {
            Status += $"；另有 {rejected.Count} 个没加进来：{string.Join("；", rejected)}";
        }
    }

    /// <summary>轮询一个导入任务直到结束，返回是否成功。
    /// 多个目录同时导入时这个方法会被并发跑多份，所以只碰自己那一行 item，不写共用的 Status。</summary>
    private async Task<bool> WatchJobAsync(string jobId, SourceItemViewModel item)
    {
        while (_api is not null)
        {
            var job = await _api.GetJobAsync(jobId);
            item.Progress = job.Total <= 0 ? 0 : 100.0 * job.Done / job.Total;

            if (job.State == "queued")
            {
                item.IsImporting = true;
                item.Status = "排队中…";
                await Task.Delay(500);
                continue;
            }
            if (job.State == "running")
            {
                item.IsImporting = true;
                var stage = job.Message.Length == 0 ? "准备中" : job.Message;
                item.Status = $"{stage}（{job.Done}/{job.Total}）";
                await Task.Delay(500);
                continue;
            }

            item.IsImporting = false;
            if (job.State == "failed")
            {
                item.Status = $"失败：{job.Error}";
                return false;
            }

            var result = job.Result;
            item.Status = result is { Skipped: true }
                ? $"✓ 已跳过（{result.Reason}），库里 {result.ChunkCount} 块"
                : $"✓ 导入完成：{result?.ChunkCount ?? 0} 块，{result?.ImageCount ?? 0} 块含图，"
                  + $"${result?.CostUsd ?? 0:0.0000}，{result?.Seconds ?? 0:0.0}s";
            if (result is not null)
            {
                // 占位 id 换成后端真正的数据源 id：随后的刷新会「原地更新这一行」，
                // 而不是又添一行、把占位行永远留在列表里（那一行是删不掉的）
                item.AdoptSourceId(result.SourceId);
            }
            return true;
        }
        return false;
    }

    /// <summary>移除数据源（连同它的分块）。</summary>
    [RelayCommand]
    private async Task RemoveSourceAsync(SourceItemViewModel? item)
    {
        if (_api is null || item is null || item.Dto.SourceId.StartsWith("pending:", StringComparison.Ordinal))
        {
            return;
        }
        try
        {
            await _api.DeleteSourceAsync(item.Dto.SourceId);
            Sources.Remove(item);
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "移除数据源");
            Status = $"移除失败：{ex.Message}";
        }
    }

    // ---------------------------------------------------------------- 会话

    /// <summary>刷新会话列表：已有行原地更新，新的插入，消失的移除；当前会话尽量保持不变。</summary>
    private async Task RefreshSessionsAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            var rows = await _api.ListSessionsAsync();
            var currentId = CurrentSession?.SessionId;
            // 列表按「最近使用」倒序，这里就按后端给的顺序摆放
            for (var index = 0; index < rows.Count; index++)
            {
                var row = rows[index];
                var existing = Sessions.FirstOrDefault(item => item.SessionId == row.SessionId);
                if (existing is null)
                {
                    Sessions.Insert(index, new SessionItemViewModel(row));
                }
                else
                {
                    existing.Update(row);
                    var from = Sessions.IndexOf(existing);
                    if (from != index)
                    {
                        Sessions.Move(from, index);
                    }
                }
            }
            foreach (var stale in Sessions.Where(item => rows.All(row => row.SessionId != item.SessionId)).ToList())
            {
                Sessions.Remove(stale);
            }
            if (currentId is not null)
            {
                CurrentSession = Sessions.FirstOrDefault(item => item.SessionId == currentId) ?? Sessions.FirstOrDefault();
            }
            ApplyGroupNames();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "刷新会话列表");
        }
    }

    /// <summary>启动时保证至少有一个会话可选（一个都没有就新建一个）。</summary>
    private async Task EnsureSessionsAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            await RefreshSessionsAsync();
            if (Sessions.Count == 0)
            {
                var created = await _api.CreateSessionAsync();
                Sessions.Add(new SessionItemViewModel(created));
            }
            if (CurrentSession is null)
            {
                CurrentSession = Sessions[0];
            }
            else
            {
                await LoadMessagesAsync(CurrentSession);   // 有可能是刚建的，装载一次消息（空列表）
            }
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "准备会话");
            Status = $"会话初始化失败：{ex.Message}";
        }
    }

    /// <summary>把某个会话的聊天记录装进界面（切换会话/启动时用）。</summary>
    private async Task LoadMessagesAsync(SessionItemViewModel session)
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            var rows = await _api.ListMessagesAsync(session.SessionId);
            Messages.Clear();
            foreach (var row in rows)
            {
                var message = new MessageViewModel(isUser: row.Role == "user", content: row.Content)
                {
                    IsStreaming = false,
                };
                if (row.Sources.Count > 0)
                {
                    message.SetSources(row.Sources);
                }
                Messages.Add(message);
            }
            Status = $"会话「{session.Title}」：{rows.Count} 条消息";
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "装载会话消息");
            Status = $"装载会话失败：{ex.Message}";
        }
    }

    /// <summary>切换会话：把那个会话的记录换上来（历史在后端，前端只管显示）。</summary>
    partial void OnCurrentSessionChanged(SessionItemViewModel? oldValue, SessionItemViewModel? newValue)
    {
        if (_switchingSession || newValue is null || newValue.SessionId == oldValue?.SessionId)
        {
            return;
        }
        SyncScopeToSession(newValue);
        _ = LoadMessagesSafeAsync(newValue);
    }

    /// <summary>把对话区那个「检索范围」下拉对齐到当前会话绑定的分组（程序化改动，不触发保存）。</summary>
    private void SyncScopeToSession(SessionItemViewModel session)
    {
        _syncingScope = true;
        try
        {
            SelectedScope = ScopeOptions.FirstOrDefault(option => option.GroupId == session.Dto.GroupId)
                            ?? ScopeOptions.FirstOrDefault();
        }
        finally
        {
            _syncingScope = false;
        }
        UpdateScopeWarning();
    }

    private async Task LoadMessagesSafeAsync(SessionItemViewModel session)
    {
        _switchingSession = true;
        try
        {
            await LoadMessagesAsync(session);
        }
        finally
        {
            _switchingSession = false;
        }
    }

    /// <summary>新建会话并切过去。</summary>
    [RelayCommand]
    private async Task NewSessionAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            // 新建的会话默认沿用左栏当前筛选的分组（选了「全部数据源」或「未分组」就是全部数据源）
            var groupId = SelectedGroupFilter?.Kind == GroupScopeKind.Group ? SelectedGroupFilter.GroupId : null;
            var created = await _api.CreateSessionAsync(null, groupId);
            var item = new SessionItemViewModel(created);
            Sessions.Insert(0, item);
            _switchingSession = false;
            CurrentSession = item;
            ApplyGroupNames();
            Messages.Clear();
            Status = groupId is null
                ? "已新建会话，直接提问即可（标题会按第一个问题自动生成）"
                : $"已新建会话，检索范围：{Groups.FirstOrDefault(g => g.GroupId == groupId)?.Name}";
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "新建会话");
            Status = $"新建会话失败：{ex.Message}";
        }
    }

    /// <summary>删除会话：第一次点进入确认态，再点一次才真删（连聊天记录一起）。</summary>
    [RelayCommand]
    private async Task DeleteSessionAsync(SessionItemViewModel? item)
    {
        if (_api is null || item is null)
        {
            return;
        }
        if (!item.IsConfirmingDelete)
        {
            foreach (var other in Sessions)
            {
                other.IsConfirmingDelete = false;   // 同时只允许一行处于确认态
            }
            item.IsConfirmingDelete = true;
            return;
        }

        try
        {
            await _api.DeleteSessionAsync(item.SessionId);
            var wasCurrent = CurrentSession?.SessionId == item.SessionId;
            Sessions.Remove(item);
            Status = $"已删除会话「{item.Title}」";
            if (wasCurrent)
            {
                // 删掉的正是当前会话：切到第一个；一个都不剩就新建一个
                if (Sessions.Count == 0)
                {
                    await NewSessionAsync();
                }
                else
                {
                    CurrentSession = Sessions[0];
                }
            }
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "删除会话");
            Status = $"删除会话失败：{ex.Message}";
            item.IsConfirmingDelete = false;
        }
    }

    /// <summary>进入重命名状态（点 ✎ 或双击标题）。</summary>
    [RelayCommand]
    private void BeginRename(SessionItemViewModel? item)
    {
        if (item is null)
        {
            return;
        }
        item.IsConfirmingDelete = false;
        item.IsRenaming = true;
    }

    /// <summary>取消删除确认（点「取消」）。</summary>
    [RelayCommand]
    private void CancelDelete(SessionItemViewModel? item)
    {
        if (item is not null)
        {
            item.IsConfirmingDelete = false;
        }
    }

    /// <summary>提交重命名（输入框里回车时调用）。</summary>
    [RelayCommand]
    private async Task CommitRenameAsync(SessionItemViewModel? item)
    {
        if (_api is null || item is null)
        {
            return;
        }
        var title = item.Title.Trim();
        item.IsRenaming = false;
        if (title.Length == 0)
        {
            item.Title = item.Dto.Title;   // 空标题视为放弃
            return;
        }
        try
        {
            await _api.RenameSessionAsync(item.SessionId, title);
            item.Update(item.Dto with { Title = title });
            Status = $"已重命名为「{title}」";
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "重命名会话");
            item.Title = item.Dto.Title;
            Status = $"重命名失败：{ex.Message}";
        }
    }

    /// <summary>放弃重命名（Esc 或点别处）。</summary>
    [RelayCommand]
    private void CancelRename(SessionItemViewModel? item)
    {
        if (item is null)
        {
            return;
        }
        item.IsRenaming = false;
        item.Title = item.Dto.Title;
    }

    /// <summary>打开设置窗口，改完立刻保存并生效（后端会写进 .env，无需重启）。</summary>
    [RelayCommand]
    private async Task OpenSettingsAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            var current = await _api.GetSettingsAsync();
            var update = await _settingsDialog.ShowSettingsAsync(current);
            if (update is null)
            {
                return; // 用户取消
            }
            var saved = await _api.SaveSettingsAsync(update);
            Status = $"设置已保存并生效（回答模型 {saved.ChatModel}）";
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "保存设置");
            Status = $"保存设置失败：{ex.Message}";
            Messages.Add(new MessageViewModel(isUser: false, content: $"⚠️ 保存设置失败：{ex.Message}"));
        }
    }

    /// <summary>清空当前会话的聊天记录（会话本身保留）。</summary>
    [RelayCommand]
    private async Task ClearContextAsync()
    {
        if (_api is null || CurrentSession is null)
        {
            return;
        }
        try
        {
            var result = await _api.ClearChatAsync(CurrentSession.SessionId);
            Messages.Clear();
            Status = $"已清空当前会话（{result.Cleared} 条消息 / {result.Turns} 轮）";
            await RefreshSessionsAsync();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "清空上下文");
            Status = $"清空失败：{ex.Message}";
        }
    }

    /// <summary>发送提问，边收边显示。</summary>
    [RelayCommand]
    private async Task SendAsync()
    {
        var question = Question.Trim();
        if (_api is null || question.Length == 0 || IsAsking)
        {
            return;
        }
        if (CurrentSession is null)
        {
            await NewSessionAsync();          // 兜底：没有会话就先建一个
            if (CurrentSession is null)
            {
                return;
            }
        }

        Question = "";
        Messages.Add(new MessageViewModel(isUser: true, content: question));
        var answer = new MessageViewModel(isUser: false, content: "") { IsStreaming = true };
        Messages.Add(answer);

        _streaming = answer;
        _pending.Clear();
        _flushedLength = 0;
        _sinceFlush.Restart();
        _chatCts = new CancellationTokenSource();
        IsAsking = true;
        Status = "检索中…";

        try
        {
            await foreach (var ev in _api.StreamChatAsync(question, CurrentSession.SessionId, _chatCts.Token))
            {
                switch (ev)
                {
                    case ChatSourcesEvent sources:
                        answer.SetSources(sources.Items);
                        Status = sources.Items.Count == 0 ? "没有检索到片段" : $"命中 {sources.Items.Count} 条片段，生成中…";
                        break;
                    case ChatTokenEvent token:
                        _pending.Append(token.Text);
                        Flush(force: false);
                        break;
                    case ChatDoneEvent done:
                        answer.Meta = $"{done.Seconds:0.0}s　prompt {done.Usage.PromptTokens} / completion "
                                      + $"{done.Usage.CompletionTokens} token　${done.Usage.CostUsd:0.000000}";
                        Status = "完成";
                        break;
                    case ChatErrorEvent error:
                        _pending.Append($"\n\n⚠️ {error.Message}");
                        Flush(force: true);
                        Status = "出错";
                        break;
                }
            }
        }
        catch (OperationCanceledException)
        {
            _pending.Append("\n\n（已取消）");
            Status = "已取消";
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "提问");
            _pending.Append($"\n\n⚠️ {ex.Message}");
            Status = $"提问失败：{ex.Message}";
        }
        finally
        {
            Flush(force: true);
            answer.IsStreaming = false;
            IsAsking = false;
            _streaming = null;
            _chatCts?.Dispose();
            _chatCts = null;
            await RefreshSessionsAsync();   // 首问会改标题、会话顺序也会变，这里同步一下
        }
    }

    /// <summary>取消正在进行的回答。</summary>
    [RelayCommand]
    private void Cancel() => _chatCts?.Cancel();

    /// <summary>把攒下的文本写回界面（按字数/时间节流）。</summary>
    private void Flush(bool force)
    {
        if (_streaming is null)
        {
            return;
        }
        if (!force && _pending.Length - _flushedLength < FlushChars && _sinceFlush.Elapsed < FlushInterval)
        {
            return;
        }
        _streaming.Content = _pending.ToString();
        _flushedLength = _pending.Length;
        _sinceFlush.Restart();
        StreamTicked?.Invoke();
    }
}
