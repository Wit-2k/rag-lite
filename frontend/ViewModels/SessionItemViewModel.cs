// 会话栏里的一行：标题可编辑、删前要确认一次（避免误删）。
using CommunityToolkit.Mvvm.ComponentModel;
using frontend.Models;

namespace frontend.ViewModels;

public partial class SessionItemViewModel : ObservableObject
{
    public SessionItemViewModel(SessionDto dto)
    {
        Dto = dto;
        Title = dto.Title;
    }

    public SessionDto Dto { get; private set; }

    public string SessionId => Dto.SessionId;

    /// <summary>标题（可编辑：正在重命名时与 Dto 暂时不一致，取消就回退）。</summary>
    [ObservableProperty]
    public partial string Title { get; set; }

    /// <summary>正在改名（界面换成输入框）。</summary>
    [ObservableProperty]
    public partial bool IsRenaming { get; set; }

    /// <summary>第一次点删除只是进入「确认」状态，再点一次才真删（列表里就地确认，不弹窗）。</summary>
    [ObservableProperty]
    public partial bool IsConfirmingDelete { get; set; }

    /// <summary>行尾的备注：消息条数。</summary>
    public string Subtitle => $"{Dto.MessageCount} 条";

    /// <summary>刷新后原地更新（保留同一个实例，避免列表闪烁）。</summary>
    public void Update(SessionDto dto)
    {
        Dto = dto;
        if (!IsRenaming)
        {
            Title = dto.Title;   // 正在改名时别把用户输入覆盖掉
        }
        OnPropertyChanged(nameof(Subtitle));
    }
}
