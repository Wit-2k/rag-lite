using frontend.Models;

// 选目录对话框的抽象：ViewModel 只认这个接口，具体实现放在窗口里（用 Avalonia 的 StorageProvider），
// 这样 ViewModel 不必知道任何 UI 类型。
namespace frontend.Services;


public interface IFolderPicker
{
    /// <summary>弹出「选择文件夹」对话框，可多选；用户取消时返回空列表。</summary>
    Task<IReadOnlyList<string>> PickFoldersAsync(string title);
}


/// <summary>
/// 设置对话框。ViewModel 只认这个接口，窗口负责实现；对话框本身不发网络请求——
/// 它只编辑传进来的 DTO，保存由 ViewModel 调 ApiClient 完成。
/// </summary>
public interface ISettingsDialog
{
    /// <summary>弹出设置窗口；用户取消返回 null。</summary>
    Task<SettingsUpdateDto?> ShowSettingsAsync(SettingsDto current);
}


/// <summary>
/// 分组管理对话框（新建 / 改名 / 删除）。
/// 与设置对话框不同，这个窗口里的每次改动都会**立刻**打后端：删分组失败（还有会话在用）时
/// 必须当场告诉用户原因，攒到最后再保存就没法给出这种反馈了。改动成功后通过 onChanged 通知主界面刷新。
/// </summary>
public interface IGroupsDialog
{
    /// <summary>弹出分组管理窗口；`onChanged` 在每次改动成功后调用（可能被调多次）。</summary>
    Task ShowDialogAsync(ApiClient api, Func<Task> onChanged);
}
