// 「分组管理」窗口的 ViewModel：新建 / 改名 / 删除分组，并在组内添加或移出数据源。
// 每次改动都立刻打后端——删不掉（还有会话在用）或添加失败时要当场把原因给用户看。
using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using frontend.Models;
using frontend.Services;

namespace frontend.ViewModels;

/// <summary>分组里的一位成员：带上它所属的分组 id，移出时才知道从哪个组里删。</summary>
public sealed class GroupSourceViewModel
{
    public GroupSourceViewModel(string groupId, SourceDto source)
    {
        GroupId = groupId;
        Source = source;
    }

    public string GroupId { get; }

    public SourceDto Source { get; }

    public string Name => Source.Name;
}

/// <summary>对话框里的一行分组，带成员清单和「可添加的数据源」。</summary>
public partial class GroupRowViewModel : ObservableObject
{
    public GroupRowViewModel(GroupDto dto, IReadOnlyList<SourceDto> sources)
    {
        Dto = dto;
        Name = dto.Name;

        // 成员与可添加项都在打开窗口时一次性算好；添加/移出后 ViewModel 会重新拉取并重建这一行
        foreach (var source in sources.Where(item => item.GroupIds.Contains(dto.GroupId)))
        {
            Members.Add(new GroupSourceViewModel(dto.GroupId, source));
        }
        foreach (var source in sources.Where(item => !item.GroupIds.Contains(dto.GroupId)))
        {
            AvailableSources.Add(source);
        }
        SelectedSource = AvailableSources.FirstOrDefault();
    }

    public GroupDto Dto { get; private set; }

    public string GroupId => Dto.GroupId;

    /// <summary>名字（可在框里改，点「改名」才保存）。</summary>
    [ObservableProperty]
    public partial string Name { get; set; }

    /// <summary>第一次点「删除」只是进入确认态，再点一次才真删。</summary>
    [ObservableProperty]
    public partial bool IsConfirmingDelete { get; set; }

    /// <summary>组内成员。</summary>
    public ObservableCollection<GroupSourceViewModel> Members { get; } = [];

    /// <summary>还没加入这个分组的数据源，供下拉选择。</summary>
    public ObservableCollection<SourceDto> AvailableSources { get; } = [];

    /// <summary>「添加数据源」下拉当前选中的源。</summary>
    [ObservableProperty]
    public partial SourceDto? SelectedSource { get; set; }

    public string Subtitle => $"{Dto.SourceCount} 个数据源";

    public bool HasMembers => Members.Count > 0;

    public bool HasAvailable => AvailableSources.Count > 0;

    public string MemberHint => HasMembers ? "" : "还没有数据源，从下面选一个加进来。";

    public string AvailableHint => HasAvailable ? "" : "所有数据源都已加入这个分组。";
}

public partial class GroupsViewModel : ObservableObject
{
    private readonly ApiClient _api;
    private readonly Func<Task> _onChanged;

    public GroupsViewModel(ApiClient api, Func<Task> onChanged)
    {
        _api = api;
        _onChanged = onChanged;
    }

    public ObservableCollection<GroupRowViewModel> Groups { get; } = [];

    /// <summary>「新建分组」输入框里的名字。</summary>
    [ObservableProperty]
    public partial string NewName { get; set; } = "";

    /// <summary>出错信息（例如「还有 1 个会话在用这个分组」）。</summary>
    [ObservableProperty]
    public partial string Error { get; set; } = "";

    public bool HasError => Error.Length > 0;

    partial void OnErrorChanged(string value) => OnPropertyChanged(nameof(HasError));

    public bool IsEmpty => Groups.Count == 0;

    /// <summary>窗口打开时拉一次分组与数据源，给每组算出成员和可添加项。</summary>
    public async Task LoadAsync()
    {
        try
        {
            var groupsTask = _api.ListGroupsAsync();
            var sourcesTask = _api.ListSourcesAsync();
            await Task.WhenAll(groupsTask, sourcesTask);

            var groups = await groupsTask;
            var sources = await sourcesTask;
            Groups.Clear();
            foreach (var row in groups)
            {
                Groups.Add(new GroupRowViewModel(row, sources));
            }
            OnPropertyChanged(nameof(IsEmpty));
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, "读取分组");
            Error = $"读取分组失败：{ex.Message}";
        }
    }

    [RelayCommand]
    private async Task CreateAsync()
    {
        var name = NewName.Trim();
        if (name.Length == 0)
        {
            Error = "先给分组起个名字";
            return;
        }
        await RunAsync(async () =>
        {
            await _api.CreateGroupAsync(name);
            NewName = "";
        }, "新建分组");
    }

    [RelayCommand]
    private async Task RenameAsync(GroupRowViewModel? row)
    {
        if (row is null)
        {
            return;
        }
        var name = row.Name.Trim();
        if (name.Length == 0)
        {
            Error = "分组名不能为空";
            return;
        }
        if (name == row.Dto.Name)
        {
            return;   // 没改就别打接口
        }
        await RunAsync(() => _api.RenameGroupAsync(row.GroupId, name), "改名");
    }

    /// <summary>删除：第一次点进入确认态，再点一次才真删（跟会话栏一个手感）。</summary>
    [RelayCommand]
    private async Task DeleteAsync(GroupRowViewModel? row)
    {
        if (row is null)
        {
            return;
        }
        if (!row.IsConfirmingDelete)
        {
            foreach (var other in Groups)
            {
                other.IsConfirmingDelete = false;   // 同时只允许一行处于确认态
            }
            row.IsConfirmingDelete = true;
            Error = "";
            return;
        }
        await RunAsync(() => _api.DeleteGroupAsync(row.GroupId), "删除分组");
    }

    [RelayCommand]
    private void CancelDelete(GroupRowViewModel? row)
    {
        if (row is not null)
        {
            row.IsConfirmingDelete = false;
        }
    }

    /// <summary>把下拉里选中的数据源加入当前分组；保留它在其他分组里的归属。</summary>
    [RelayCommand]
    private async Task AddSourceAsync(GroupRowViewModel? row)
    {
        if (row?.SelectedSource is null)
        {
            Error = "先从下拉里选一个数据源";
            return;
        }
        var source = row.SelectedSource;
        var groupIds = source.GroupIds.Append(row.GroupId).Distinct().ToList();
        await RunAsync(() => _api.SetSourceGroupsAsync(source.SourceId, groupIds), "添加数据源");
    }

    /// <summary>把某个数据源移出它所在的分组；其他分组的归属保持不变。</summary>
    [RelayCommand]
    private async Task RemoveSourceAsync(GroupSourceViewModel? member)
    {
        if (member is null)
        {
            return;
        }
        var groupIds = member.Source.GroupIds.Where(id => id != member.GroupId).ToList();
        await RunAsync(() => _api.SetSourceGroupsAsync(member.Source.SourceId, groupIds), "移出数据源");
    }

    /// <summary>跑一次写操作：清掉旧错误 → 打接口 → 重新拉列表 → 通知主界面。</summary>
    private async Task RunAsync(Func<Task> action, string what)
    {
        Error = "";
        try
        {
            await action();
            await LoadAsync();
            await _onChanged();
        }
        catch (Exception ex)
        {
            AppLog.Error(ex, what);
            Error = ex.Message;   // 后端 409 的 detail 就是「还有哪些会话在用」，原样显示
        }
    }
}
