// 左侧列表里的一行数据源，额外带上「导入进度」「所属分组」这些只属于界面状态的东西。
using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using frontend.Models;

namespace frontend.ViewModels;

public partial class SourceItemViewModel : ObservableObject
{
    private readonly Func<SourceItemViewModel, Task> _onGroupsChanged;
    private readonly Dictionary<string, string> _groupNames = [];

    public SourceItemViewModel(SourceDto dto, Func<SourceItemViewModel, Task>? onGroupsChanged = null)
    {
        Dto = dto;
        // 勾选分组后由主界面的 ViewModel 负责调接口保存（这里只管界面状态）
        _onGroupsChanged = onGroupsChanged ?? (_ => Task.CompletedTask);
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

    /// <summary>「分组」按钮弹出的小清单里的一项（勾上就属于这个分组）。</summary>
    public ObservableCollection<GroupToggleViewModel> GroupToggles { get; } = [];

    /// <summary>卡片上显示的所属分组（例如「分组：民法、重点」；一个都没选就是「未分组」）。</summary>
    [ObservableProperty]
    public partial string GroupSummary { get; set; } = "";

    /// <summary>一个分组都还没有时为 true（卡片上的小清单换成一句提示）。</summary>
    [ObservableProperty]
    public partial bool NoGroupsYet { get; set; }

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

    /// <summary>
    /// 分组列表刷新后调用：把勾选清单对齐到最新的分组集合。
    /// 只有「分组集合本身变了」才重建清单——不然导入期间每 500ms 一次刷新会把
    /// 用户正打开的清单重建一遍（勾选到一半就没了）。
    /// </summary>
    public void SyncGroupToggles(IReadOnlyList<GroupDto> groups)
    {
        _groupNames.Clear();
        foreach (var group in groups)
        {
            _groupNames[group.GroupId] = group.Name;
        }

        var wanted = groups.Select(group => group.GroupId).ToList();
        if (!wanted.SequenceEqual(GroupToggles.Select(toggle => toggle.GroupId)))
        {
            GroupToggles.Clear();
            foreach (var group in groups)
            {
                GroupToggles.Add(new GroupToggleViewModel(
                    group.GroupId, group.Name, Dto.GroupIds.Contains(group.GroupId), OnToggleChanged));
            }
        }

        // 打勾状态按库里的事实对齐（重建后第一次也要走这里）
        foreach (var toggle in GroupToggles)
        {
            toggle.IsChecked = Dto.GroupIds.Contains(toggle.GroupId);
        }
        NoGroupsYet = groups.Count == 0;
        UpdateGroupSummary();
    }

    /// <summary>某个分组被改名/删除后，卡片上的文字也得跟着变。</summary>
    public void RefreshGroupSummary(IReadOnlyList<GroupDto> groups)
    {
        _groupNames.Clear();
        foreach (var group in groups)
        {
            _groupNames[group.GroupId] = group.Name;
        }
        // 分组被删掉时，后端已经把成员关系一起清了；这里只显示还存在的
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

    private Task OnToggleChanged() => _onGroupsChanged(this);

    /// <summary>勾选清单里勾了/取消了某一项时，把当前勾中的分组 id 交给主界面去保存。</summary>
    public IReadOnlyList<string> CheckedGroupIds =>
        GroupToggles.Where(toggle => toggle.IsChecked).Select(toggle => toggle.GroupId).ToList();

    /// <summary>导入成功后，把占位 id（pending:jobId）换成后端真正的数据源 id。
    /// 换完之后刷新就会「原地更新这一行」：既不会多出一行，也不会留下删不掉的占位行
    /// （占位行的 id 后端不认识，移除按钮对它是无效的）。Status 里的「已跳过」等提示会保留。</summary>
    public void AdoptSourceId(string sourceId)
    {
        Dto = Dto with { SourceId = sourceId };
    }
}

/// <summary>「分组」小清单里的一项：勾上就代表这个数据源属于该分组。</summary>
public partial class GroupToggleViewModel : ObservableObject
{
    private readonly Func<Task> _onChanged;

    public GroupToggleViewModel(string groupId, string name, bool isChecked, Func<Task> onChanged)
    {
        GroupId = groupId;
        Name = name;
        _onChanged = onChanged;
        IsChecked = isChecked;
    }

    public string GroupId { get; }

    public string Name { get; }

    /// <summary>勾选状态。界面上是**单向**绑定（见 MainWindow.axaml）：
    /// 双向绑定会在控件卸载时把默认值写回来，于是「刷新重建清单」会误触发一次保存
    /// （实测踩过：新建一个分组，把已有数据源的成员关系清空了）。</summary>
    [ObservableProperty]
    public partial bool IsChecked { get; set; }

    /// <summary>用户点击时才走这里：先反转状态，再交给主界面保存。</summary>
    [RelayCommand]
    private void Toggle()
    {
        IsChecked = !IsChecked;
        _ = _onChanged();
    }
}
