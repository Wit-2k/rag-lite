// 「分组管理」窗口的 ViewModel：新建 / 改名 / 删除数据源分组。
// 每次改动都立刻打后端——删不掉（还有会话在用）时要当场把原因给用户看。
using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using frontend.Models;
using frontend.Services;

namespace frontend.ViewModels;

/// <summary>对话框里的一行分组。</summary>
public partial class GroupRowViewModel : ObservableObject
{
    public GroupRowViewModel(GroupDto dto)
    {
        Dto = dto;
        Name = dto.Name;
    }

    public GroupDto Dto { get; private set; }

    public string GroupId => Dto.GroupId;

    /// <summary>名字（可在框里改，点「改名」才保存）。</summary>
    [ObservableProperty]
    public partial string Name { get; set; }

    /// <summary>第一次点「删除」只是进入确认态，再点一次才真删。</summary>
    [ObservableProperty]
    public partial bool IsConfirmingDelete { get; set; }

    public string Subtitle => $"{Dto.SourceCount} 个数据源";

    public void Update(GroupDto dto)
    {
        Dto = dto;
        if (Name != dto.Name)
        {
            Name = dto.Name;   // 改完名/刷新后对齐
        }
        OnPropertyChanged(nameof(Subtitle));
    }
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

    /// <summary>窗口打开时拉一次列表。</summary>
    public async Task LoadAsync()
    {
        try
        {
            var rows = await _api.ListGroupsAsync();
            Groups.Clear();
            foreach (var row in rows)
            {
                Groups.Add(new GroupRowViewModel(row));
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
