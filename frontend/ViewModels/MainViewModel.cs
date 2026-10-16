// 主界面：左边管数据源（添加目录、看导入进度），右边对话（流式回答 + 清空上下文）。
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Text;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
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
    private readonly StringBuilder _pending = new();
    private readonly Stopwatch _sinceFlush = Stopwatch.StartNew();

    private ApiClient? _api;
    private MessageViewModel? _streaming;
    private CancellationTokenSource? _chatCts;
    private int _flushedLength;
    private bool _trayHintShown;

    public MainViewModel(BackendProcess backend, IFolderPicker picker, ISettingsDialog settingsDialog)
    {
        _backend = backend;
        _picker = picker;
        _settingsDialog = settingsDialog;
    }

    [ObservableProperty]
    public partial string Status { get; set; } = "正在启动后端…";

    [ObservableProperty]
    public partial bool IsReady { get; set; }

    /// <summary>配置提示：密钥没配、有数据源需要重导时显示在对话区顶部。</summary>
    [ObservableProperty]
    public partial string ConfigHint { get; set; } = "";

    [ObservableProperty]
    public partial string Question { get; set; } = "";

    [ObservableProperty]
    public partial bool IsAsking { get; set; }

    [ObservableProperty]
    public partial HealthDto? Health { get; set; }

    public ObservableCollection<SourceItemViewModel> Sources { get; } = [];

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
            var rows = await _api.ListSourcesAsync();
            MergeSources(rows);
            IsReady = true;
            ConfigHint = BuildHint(Health);
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
            hints.Add($"还没有配密钥（缺 {string.Join("、", health.MissingKeys)}）：请在 backend/.env 填 OPENROUTER_API_KEY 后重启");
        }
        if (health.SourcesStale > 0)
        {
            hints.Add($"有 {health.SourcesStale} 个数据源是用别的嵌入模型入库的，建议在左侧重新导入");
        }
        return hints.Count == 0
            ? $"回答模型：{health.ChatModel}　嵌入：{health.EmbedModel}（{health.EmbedDim} 维）"
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
                    Stale: false))
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

    /// <summary>清空对话上下文（后端会话里的历史）。</summary>
    [RelayCommand]
    private async Task ClearContextAsync()
    {
        if (_api is null)
        {
            return;
        }
        try
        {
            var result = await _api.ClearChatAsync();
            Messages.Add(new MessageViewModel(isUser: false, content: $"（已清空上下文：{result.Cleared} 条历史消息）"));
            Status = "上下文已清空";
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
            await foreach (var ev in _api.StreamChatAsync(question, _chatCts.Token))
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
