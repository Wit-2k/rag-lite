// 左侧列表里的一行数据源，额外带上「导入进度」「只读分组摘要」这些只属于界面状态的东西。
using CommunityToolkit.Mvvm.ComponentModel;
using frontend.Models;

namespace frontend.ViewModels;

public partial class SourceItemViewModel : ObservableObject
{
    private readonly Dictionary<string, string> _groupNames = [];

    public SourceItemViewModel(SourceDto dto)
    {
        Dto = dto;
        UpdateGroupSummary();
    }

    public SourceDto Dto { get; private set; }

    public string Name => Dto.Name;

    /// <summary>一行摘要：块数 / 图数 / 维度。</summary>
    public string Subtitle => $"{Dto.ChunkCount} 块 · {Dto.ImageCount} 图 · {Dto.EmbedDim} 维";

    /// <summary>需要提醒的标记（目录没了 / 换了嵌入模型需要重新导入）。</summary>
    public string Badge => Dto switch
    {
        { Missing: true } => "⚠ 产物目录已不存在",
        { Stale: true } => $"⚠ 需重新导入（入库模型 {Dto.EmbedModel}）",
        _ => "",
    };

    public bool HasBadge => Badge.Length > 0;

    /// <summary>卡片上只读的分组摘要（例如「分组：民法、重点」；一个都没加就是「未分组」）。
    /// 成员关系统一在分组管理窗口维护，卡片上不再提供勾选入口。</summary>
    [ObservableProperty]
    public partial string GroupSummary { get; set; } = "";

    /// <summary>导入任务的进度文案，平时为空。</summary>
    [ObservableProperty]
    public partial string Status { get; set; } = "";

    public bool HasStatus => Status.Length > 0;

    partial void OnStatusChanged(string value) => OnPropertyChanged(nameof(HasStatus));

    [ObservableProperty]
    public partial bool IsImporting { get; set; }

    /// <summary>0~100，绑定进度条。</summary>
    [ObservableProperty]
    public partial double Progress { get; set; }

    /// <summary>刷新后替换内部数据（来源刷新时保留同一个 ViewModel 实例，避免列表闪烁）。</summary>
    public void Update(SourceDto dto)
    {
        Dto = dto;
        OnPropertyChanged(nameof(Name));
        OnPropertyChanged(nameof(Subtitle));
        OnPropertyChanged(nameof(Badge));
        OnPropertyChanged(nameof(HasBadge));
        UpdateGroupSummary();
    }

    /// <summary>分组列表刷新后调用：只更新只读摘要；成员关系由分组管理窗口负责保存。</summary>
    public void SyncGroups(IReadOnlyList<GroupDto> groups)
    {
        _groupNames.Clear();
        foreach (var group in groups)
        {
            _groupNames[group.GroupId] = group.Name;
        }
        UpdateGroupSummary();
    }

    private void UpdateGroupSummary()
    {
        var names = Dto.GroupIds
            .Where(_groupNames.ContainsKey)
            .Select(id => _groupNames[id])
            .ToList();
        GroupSummary = names.Count == 0 ? "分组：未分组" : $"分组：{string.Join("、", names)}";
    }

    /// <summary>导入成功后，把占位 id（pending:jobId）换成后端真正的数据源 id。
    /// 换完之后刷新就会「原地更新这一行」：既不会多出一行，也不会留下删不掉的占位行
    /// （占位行的 id 后端不认识，移除按钮对它是无效的）。Status 里的「已跳过」等提示会保留。</summary>
    public void AdoptSourceId(string sourceId)
    {
        Dto = Dto with { SourceId = sourceId };
    }
}
